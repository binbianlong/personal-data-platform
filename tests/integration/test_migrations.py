from __future__ import annotations

import shutil
from datetime import UTC, datetime
from unittest.mock import Mock

import duckdb
import pytest

from personal_data_platform.cli import main
from personal_data_platform.sources.iphone_screen_time.batch import IPhoneScreenTimeBatch
from personal_data_platform.storage.migrations import DEFAULT_MIGRATIONS
from personal_data_platform.storage.motherduck import (
    Warehouse,
    WarehouseConfig,
    connect,
)
from tests.screen_time_helpers import _raw, _record


@pytest.fixture
def warehouse():
    value = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def pending_migrations(tmp_path):
    migrations = tmp_path / "migrations"
    shutil.copytree(DEFAULT_MIGRATIONS, migrations)
    (migrations / "999_pending.sql").write_text(
        "CREATE TABLE base.migration_probe (id INTEGER); "
        "INSERT INTO base.migration_probe VALUES (1);"
    )
    return migrations


def test_migration_preserves_receipts_committed_by_another_startup(tmp_path):
    database = str(tmp_path / "concurrent.duckdb")
    connection = connect(WarehouseConfig(database))
    other = Warehouse(connect(WarehouseConfig(database)))
    committed = []
    raw = _raw()

    class InterveningConnection:
        def execute(self, sql, *parameters):
            if sql == "CREATE SCHEMA IF NOT EXISTS ops" and not committed:
                other.migrate()
                other.load_object(raw, byte_size=10, batch=IPhoneScreenTimeBatch([_record(raw)]))
                committed.extend(
                    other.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")
                )
            return connection.execute(sql, *parameters)

        def close(self):
            connection.close()

    warehouse = Warehouse(InterveningConnection())
    try:
        warehouse.migrate()
        assert (
            warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")
            == committed
        )
        assert warehouse.query_rows("SELECT event_key FROM base.iphone_screen_time_event") == [
            ("event",)
        ]
    finally:
        warehouse.close()
        other.close()


def test_repeated_migration_preserves_loaded_data_and_ledger(warehouse):
    warehouse.migrate()
    raw = _raw()
    warehouse.load_object(raw, byte_size=10, batch=IPhoneScreenTimeBatch([_record(raw)]))
    tables = (
        "ops.schema_migration",
        "ops.ingestion_metadata",
        "ops.iphone_screen_time_segment",
        "ops.iphone_screen_time_record",
        "base.iphone_screen_time_event",
    )
    before = {name: warehouse.query_rows(f"SELECT * FROM {name}") for name in tables}
    warehouse.migrate()
    assert {name: warehouse.query_rows(f"SELECT * FROM {name}") for name in tables} == before


def test_fully_applied_migration_only_reads_schema_and_ledger(warehouse):
    warehouse.migrate()
    connection = Mock(wraps=warehouse.connection)
    warehouse.connection = connection

    warehouse.migrate()

    statements = [call.args[0] for call in connection.execute.call_args_list]
    assert len(statements) == 2
    assert all(sql.startswith("SELECT ") for sql in statements)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("checksum", "changed", "applied migration changed"),
        ("migration_id", "unknown.sql", "unsupported schema history"),
    ],
)
def test_fully_applied_migration_rejects_invalid_history(warehouse, column, value, message):
    warehouse.migrate()
    warehouse.connection.execute(
        f"UPDATE ops.schema_migration SET {column} = ? WHERE migration_id = '001_initial.sql'",
        [value],
    )
    before = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")

    with pytest.raises(RuntimeError, match=message):
        warehouse.migrate()

    assert (
        warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id") == before
    )


def test_applied_sql_changes_stop_before_applying_later_migrations(warehouse, tmp_path):
    migrations = DEFAULT_MIGRATIONS
    for path in migrations.glob("*.sql"):
        shutil.copyfile(path, tmp_path / path.name)
    warehouse.migrate(tmp_path)
    before = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")
    initial = tmp_path / "001_initial.sql"
    initial.write_text(initial.read_text() + "\nSELECT 1;\n")
    (tmp_path / "002_later.sql").write_text("CREATE TABLE base.later (id INTEGER);")
    with pytest.raises(RuntimeError, match="applied migration changed"):
        warehouse.migrate(tmp_path)
    assert (
        warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id") == before
    )
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'base' AND table_name = 'later'"
        )
        == 0
    )


@pytest.mark.parametrize("initial", [True, False], ids=["initial", "forward"])
def test_failed_migration_rolls_back_schema_data_and_ledger(warehouse, tmp_path, initial):
    if initial:
        path = tmp_path / "001_initial.sql"
        sql = (DEFAULT_MIGRATIONS / path.name).read_text()
        before = []
    else:
        warehouse.migrate()
        before = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")
        for migration in DEFAULT_MIGRATIONS.glob("*.sql"):
            shutil.copyfile(migration, tmp_path / migration.name)
        path = tmp_path / "999_next.sql"
        sql = "CREATE TABLE base.next (id INTEGER); INSERT INTO base.next VALUES (1);"
    path.write_text(sql + "\nSELECT error('interrupted migration');")
    with pytest.raises(duckdb.InvalidInputException, match="interrupted migration"):
        warehouse.migrate(tmp_path)
    assert (
        warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id") == before
    )
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'base' "
            "AND table_name = ?",
            ["screen_time_event" if initial else "next"],
        )
        == 0
    )
    path.write_text(sql)
    warehouse.migrate(tmp_path)
    ledger = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")
    assert len(ledger) == len(before) + 1
    if not initial:
        assert warehouse.query_rows("SELECT * FROM base.next") == [(1,)]
    warehouse.migrate(tmp_path)
    assert (
        warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id") == ledger
    )


def test_pending_migration_preserves_active_writer_and_retries_after_release(
    warehouse, pending_migrations
):
    warehouse.migrate()
    warehouse.acquire_job_lock("loader", "collector", lease_seconds=7500)
    lease = warehouse.query_rows("SELECT * FROM ops.job_lock")
    ledger = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")

    with pytest.raises(RuntimeError, match="writer lease"):
        warehouse.migrate(pending_migrations)

    assert warehouse.query_rows("SELECT * FROM ops.job_lock") == lease
    assert (
        warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id") == ledger
    )
    warehouse.release_job_lock("loader", "collector")
    warehouse.migrate(pending_migrations)
    assert warehouse.query_rows("SELECT * FROM ops.job_lock") == []
    assert warehouse.query_rows("SELECT * FROM base.migration_probe") == [(1,)]


@pytest.mark.parametrize("stage", ["apply", "commit"])
def test_migration_excludes_new_writers_until_commit(tmp_path, pending_migrations, stage):
    database = str(tmp_path / "concurrent.duckdb")
    warehouse = Warehouse(connect(WarehouseConfig(database)))
    warehouse.migrate()
    writer = Warehouse(connect(WarehouseConfig(database)))
    connection = warehouse.connection
    attempted = []
    raw = _raw()

    class ConcurrentWriter:
        applying = False

        def execute(self, sql, *args):
            apply = "CREATE TABLE base.migration_probe" in sql
            if apply:
                self.applying = True
            if not attempted and (
                (stage == "apply" and apply)
                or (stage == "commit" and self.applying and sql == "COMMIT")
            ):
                acquired = writer.acquire_job_lock("loader", "worker", lease_seconds=7500)
                attempted.append(acquired)
                if acquired:
                    writer.load_object(
                        raw, byte_size=10, batch=IPhoneScreenTimeBatch([_record(raw)])
                    )
                    writer.release_job_lock("loader", "worker")
            return connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(connection, name)

    warehouse.connection = ConcurrentWriter()
    try:
        warehouse.migrate(pending_migrations)
        assert attempted == [False]
        assert writer.acquire_job_lock("loader", "worker", lease_seconds=7500)
        writer.load_object(raw, byte_size=10, batch=IPhoneScreenTimeBatch([_record(raw)]))
        writer.release_job_lock("loader", "worker")
        assert writer.succeeded_keys_for([raw]) == {raw.key}
        assert writer.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 1
    finally:
        warehouse.close()
        writer.close()


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_migration_commit_stops_writes_until_reconnected(
    tmp_path, pending_migrations, committed
):
    database = str(tmp_path / "uncertain.duckdb")
    warehouse = Warehouse(connect(WarehouseConfig(database)))
    warehouse.migrate()
    raw = _raw()
    warehouse.load_object(raw, byte_size=10, batch=IPhoneScreenTimeBatch([_record(raw)]))
    connection = warehouse.connection
    after_failure = []

    class UncertainCommit:
        applying = False
        failed = False

        def execute(self, sql, *args):
            if self.failed:
                after_failure.append(sql)
            if "CREATE TABLE base.migration_probe" in sql:
                self.applying = True
            if self.applying and sql == "COMMIT":
                if committed:
                    connection.execute(sql, *args)
                self.failed = True
                raise OSError("migration response lost")
            return connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(connection, name)

    warehouse.connection = UncertainCommit()
    try:
        with pytest.raises(OSError, match="migration response lost"):
            warehouse.migrate(pending_migrations)
        assert warehouse.connection_usable is False
        assert after_failure == []
    finally:
        warehouse.close()
    warehouse = Warehouse(connect(WarehouseConfig(database)))
    try:
        assert warehouse.query_value(
            "SELECT count(*) FROM ops.schema_migration WHERE migration_id='999_pending.sql'"
        ) == int(committed)
        assert warehouse.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 1
        assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 1
        warehouse.connection.execute(
            "UPDATE ops.job_lock SET expires_at=current_timestamp - INTERVAL '1 second'"
        )
        warehouse.migrate(pending_migrations)
        assert warehouse.succeeded_keys_for([raw]) == {raw.key}
        assert warehouse.acquire_job_lock("loader", "retry", lease_seconds=120)
        assert (
            warehouse.load_object(raw, byte_size=10, batch=IPhoneScreenTimeBatch([_record(raw)]))
            == 0
        )
        warehouse.release_job_lock("loader", "retry")
        assert warehouse.query_rows("SELECT * FROM ops.job_lock") == []
    finally:
        warehouse.close()


def test_unexpected_history_columns_are_rejected_without_modifying_it(warehouse):
    warehouse.connection.execute(
        "CREATE SCHEMA ops; CREATE TABLE ops.schema_migration "
        "(migration_id VARCHAR, checksum VARCHAR, applied_at TIMESTAMPTZ, extra_column VARCHAR)"
    )
    warehouse.connection.execute(
        "INSERT INTO ops.schema_migration VALUES ('001_initial.sql', 'preserved', now(), 'extra')"
    )
    before = warehouse.query_rows("SELECT * FROM ops.schema_migration")
    with pytest.raises(RuntimeError, match="rebuild.*empty"):
        warehouse.migrate()
    assert warehouse.query_rows("SELECT * FROM ops.schema_migration") == before
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='base'"
        )
        == 0
    )


@pytest.mark.parametrize("empty_ledger", [False, True])
def test_untracked_existing_schema_requires_rebuild(warehouse, empty_ledger):
    warehouse.connection.execute("CREATE SCHEMA base; CREATE TABLE base.previous (id INTEGER)")
    if empty_ledger:
        warehouse.connection.execute(
            "CREATE SCHEMA ops; CREATE TABLE ops.schema_migration "
            "(migration_id VARCHAR, checksum VARCHAR, applied_at TIMESTAMPTZ)"
        )
    with pytest.raises(RuntimeError, match="rebuild.*empty"):
        warehouse.migrate()
    assert warehouse.query_value(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='schema_migration'"
    ) == int(empty_ledger)


def test_fresh_target_ignores_an_attached_database(warehouse):
    warehouse.connection.execute("ATTACH ':memory:' AS unrelated")
    warehouse.connection.execute(
        "CREATE SCHEMA unrelated.ops; CREATE TABLE unrelated.ops.schema_migration "
        "(migration_id VARCHAR, checksum VARCHAR, applied_at TIMESTAMPTZ, extra_column VARCHAR)"
    )
    warehouse.connection.execute(
        "INSERT INTO unrelated.ops.schema_migration VALUES ('other.sql','preserved',now(),'extra')"
    )
    before = warehouse.query_rows("SELECT * FROM unrelated.ops.schema_migration")
    warehouse.migrate()
    warehouse.migrate()
    assert warehouse.query_value("SELECT count(*) FROM ops.schema_migration") == len(
        list(DEFAULT_MIGRATIONS.glob("*.sql"))
    )
    assert warehouse.query_rows("SELECT * FROM unrelated.ops.schema_migration") == before


def test_platform_migrate_initializes_a_local_database(tmp_path):
    database = tmp_path / "local.duckdb"
    assert main(["migrate", "--database", str(database)]) == 0
    with duckdb.connect(str(database)) as connection:
        assert connection.execute(
            "SELECT migration_id FROM ops.schema_migration ORDER BY migration_id"
        ).fetchall() == [(path.name,) for path in sorted(DEFAULT_MIGRATIONS.glob("*.sql"))]
        assert connection.execute(
            "SELECT count(*) FROM base.fitbit_activity_interval"
        ).fetchone() == (0,)


def test_platform_migrate_rejects_a_nonlocal_database(capsys):
    assert main(["migrate", "--database", "md:production"]) == 1
    assert "local .duckdb or .db" in capsys.readouterr().err


@pytest.fixture
def legacy_screen_time(warehouse, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for path in DEFAULT_MIGRATIONS.glob("*.sql"):
        if path.name < "012":
            shutil.copyfile(path, legacy / path.name)
    warehouse.migrate(legacy)
    for stream, platform, device in [
        ("app-in-focus", "ios", "phone"),
        ("app-usage", "macos", "mac"),
    ]:
        key = f"raw/screen_time/v2/{device}/{stream}/segment/current.segb.gz"
        warehouse.connection.execute(
            "INSERT INTO ops.screen_time_segment VALUES (?, ?, 'segment', now(), ?)",
            [device, stream, key],
        )
        warehouse.connection.execute(
            "INSERT INTO ops.screen_time_record VALUES (?, ?, ?, 'segment', 1, 2, ?, 'app', "
            "now(), 'start', NULL, 1, NULL, NULL, NULL, 'parser-v1', 0)",
            [device + "-physical", device, stream, device + "-active"],
        )
        for active in [True, False]:
            warehouse.connection.execute(
                "INSERT INTO base.screen_time_event VALUES (?, ?, ?, ?, 'app', now(), 'start', "
                "1, ?, 'segment', now(), 'parser-v1', 0, 2, ?, now())",
                [
                    device + ("-active" if active else "-inactive"),
                    device,
                    platform,
                    stream,
                    key,
                    active,
                ],
            )
        warehouse.connection.execute(
            "INSERT INTO ops.ingestion_metadata "
            "(object_key,source_id,schema_version,subject_key,source_stream,logical_key,observed_at,"
            "content_sha256,byte_size,status,started_at) "
            "VALUES (?,'screen_time',2,?,?,'segment',now(),'hash',12,'succeeded',now())",
            [key, device, stream],
        )
    for name in ["screen_time_reconciliation", "screen_time_app_usage_reconciliation"]:
        warehouse.connection.execute(
            "INSERT INTO ops.heartbeat VALUES (?, TIMESTAMPTZ '2026-10-01 00:00:00Z', 'run', "
            '\'{"source_id":"screen_time","last_scan_at":"2026-10-01T00:00:00Z"}\')',
            [name],
        )
    return warehouse


def test_split_migration_preserves_all_history_identity_and_constraints(legacy_screen_time):
    warehouse = legacy_screen_time
    before = warehouse.query_rows("SELECT * FROM base.screen_time_event ORDER BY event_key")
    records = warehouse.query_rows("SELECT * FROM ops.screen_time_record ORDER BY physical_id")
    physical_identity = warehouse.query_value(
        "SELECT ops.screen_time_physical_id('device','stream','segment',1,2,3.0,'digest')"
    )
    warehouse.migrate()
    for source, platform, device in [
        ("iphone_screen_time", "ios", "phone"),
        ("mac_screen_time", "macos", "mac"),
    ]:
        after = warehouse.query_rows(f"SELECT * FROM base.{source}_event ORDER BY event_key")
        expected = [
            tuple(
                value.replace("raw/screen_time/v2/", f"raw/{source}/v2/")
                if isinstance(value, str)
                else value
                for value in row
            )
            for row in before
            if row[2] == platform
        ]
        assert after == expected
        assert warehouse.query_rows(f"SELECT * FROM ops.{source}_record") == [
            row for row in records if row[1] == device
        ]
        assert warehouse.query_rows(
            "SELECT source_id, object_key FROM ops.ingestion_metadata WHERE subject_key=?", [device]
        ) == [
            (
                source,
                f"raw/{source}/v2/{device}/{'app-in-focus' if device == 'phone' else 'app-usage'}/segment/current.segb.gz",
            )
        ]
        assert warehouse.query_rows(
            f"SELECT event_key FROM ops.{source}_resolve(['{device}-active'])"
        ) == [(device + "-active",)]
        with pytest.raises(duckdb.ConstraintException):
            warehouse.connection.execute(
                f"INSERT INTO base.{source}_event SELECT * FROM base.{source}_event LIMIT 1"
            )
        with pytest.raises(duckdb.ConstraintException):
            warehouse.connection.execute(f"UPDATE base.{source}_event SET state='invalid'")
        with pytest.raises(duckdb.ConstraintException):
            warehouse.connection.execute(f"UPDATE ops.{source}_record SET parser_version=NULL")
        for table in [f"ops.{source}_record", f"ops.{source}_segment"]:
            with pytest.raises(duckdb.ConstraintException):
                warehouse.connection.execute(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1")
        assert (
            warehouse.query_value(
                f"SELECT ops.{source}_physical_id('device','stream','segment',1,2,3.0,'digest')"
            )
            == physical_identity
        )

    assert warehouse.query_rows(
        "SELECT monitor_name,succeeded_at,run_id,json_extract_string(details,'$.source_id'),json_extract_string(details,'$.last_scan_at') FROM ops.heartbeat ORDER BY monitor_name"
    ) == [
        (
            "iphone_screen_time_reconciliation",
            datetime(2026, 10, 1, tzinfo=UTC),
            "run",
            "iphone_screen_time",
            "2026-10-01T00:00:00Z",
        ),
        (
            "mac_screen_time_reconciliation",
            datetime(2026, 10, 1, tzinfo=UTC),
            "run",
            "mac_screen_time",
            "2026-10-01T00:00:00Z",
        ),
    ]
    assert (
        warehouse.query_rows(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'screen_time_%'"
        )
        == []
    )
    warehouse.migrate()
    assert warehouse.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE base.screen_time_event SET source_stream='unknown' WHERE NOT is_active",
        "UPDATE base.screen_time_event SET platform='macos' WHERE device_key='phone'",
        "UPDATE base.screen_time_event SET object_key='raw/screen_time/v1/unknown' WHERE NOT is_active",
        "UPDATE ops.screen_time_record SET source_stream='unknown'",
        "UPDATE ops.screen_time_segment SET object_key='raw/unknown'",
        "UPDATE ops.ingestion_metadata SET source_stream='unknown'",
        "UPDATE ops.ingestion_metadata SET object_key='raw/screen_time/v1/unknown' WHERE subject_key='phone'",
        "UPDATE ops.screen_time_segment SET source_stream='unknown'",
        "UPDATE base.screen_time_event SET source_stream='App.InFocus' WHERE NOT is_active",
        "CREATE VIEW marts.unexpected AS SELECT * FROM base.screen_time_event",
        "CREATE VIEW marts.unexpected AS SELECT * FROM base.screen_time_transition",
        "CREATE MACRO ops.unexpected() AS TABLE (SELECT * FROM base.screen_time_event)",
        "UPDATE ops.ingestion_metadata SET schema_version=1 WHERE source_id='screen_time'",
        "UPDATE ops.ingestion_metadata SET source_id='other' WHERE subject_key='phone'",
        "INSERT INTO ops.heartbeat VALUES ('iphone_screen_time_reconciliation', now(), 'collision', NULL)",
        "INSERT INTO ops.ingestion_metadata SELECT replace(object_key, 'raw/screen_time/', 'raw/iphone_screen_time/'), 'other', schema_version, subject_key, source_stream, logical_key, observed_at, content_sha256, byte_size, status, parser_version, record_count, started_at, completed_at, error_type, error_message, retry_count, storage_created_at, storage_generation FROM ops.ingestion_metadata WHERE subject_key='phone'",
    ],
)
def test_split_migration_rejects_unknown_partitions_without_loss(legacy_screen_time, mutation):
    warehouse = legacy_screen_time
    warehouse.connection.execute(mutation)
    before = warehouse.query_rows("SELECT * FROM base.screen_time_event ORDER BY event_key")
    with pytest.raises(duckdb.Error):
        warehouse.migrate()
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event ORDER BY event_key") == before
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM ops.schema_migration WHERE migration_id LIKE '012%'"
        )
        == 0
    )
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='iphone_screen_time_event'"
        )
        == 0
    )


def test_split_migration_removes_known_views_in_reverse_dependency_order(legacy_screen_time):
    warehouse = legacy_screen_time
    warehouse.connection.execute(
        "CREATE VIEW base.screen_time_interval AS SELECT * FROM base.screen_time_transition; "
        "CREATE VIEW marts.daily_screen_time AS SELECT * FROM base.screen_time_interval; "
        "CREATE VIEW marts.daily_screen_time_total AS SELECT * FROM marts.daily_screen_time; "
        "CREATE VIEW marts.fitbit_sleep_screen_time AS SELECT * FROM base.screen_time_interval"
    )
    warehouse.migrate()
    assert (
        warehouse.query_rows(
            "SELECT view_name FROM duckdb_views() WHERE database_name=current_database() "
            "AND schema_name IN ('base','marts') AND view_name IN "
            "('screen_time_transition','screen_time_interval','daily_screen_time',"
            "'daily_screen_time_total','fitbit_sleep_screen_time')"
        )
        == []
    )
    assert warehouse.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 2
