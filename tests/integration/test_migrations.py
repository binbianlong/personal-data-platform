from __future__ import annotations

import shutil
from dataclasses import replace
from unittest.mock import Mock

import duckdb
import pytest

from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import (
    DEFAULT_MIGRATIONS,
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
                other.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
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
        assert warehouse.query_rows("SELECT event_key FROM base.screen_time_event") == [("event",)]
    finally:
        warehouse.close()
        other.close()


def test_repeated_migration_preserves_loaded_data_and_ledger(warehouse):
    warehouse.migrate()
    raw = _raw()
    warehouse.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
    tables = (
        "ops.schema_migration",
        "ops.ingestion_metadata",
        "ops.screen_time_segment",
        "ops.screen_time_record",
        "base.screen_time_event",
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
        path = tmp_path / "003_next.sql"
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


def test_initialization_sets_iphone_and_mac_platform(warehouse):
    warehouse.migrate()
    iphone_raw = _raw()
    warehouse.load_object(iphone_raw, byte_size=10, batch=ScreenTimeBatch([_record(iphone_raw)]))
    assert warehouse.query_rows("SELECT event_key, platform FROM base.screen_time_event") == [
        ("event", "ios")
    ]

    mac_raw = replace(
        iphone_raw, key="raw/mac", subject_key="mac", stream="app-usage", logical_key="mac-segment"
    )
    mac_record = replace(_record(mac_raw), event_key="mac-event", parser_version="app-usage-v1")
    warehouse.load_object(mac_raw, byte_size=10, batch=ScreenTimeBatch([mac_record]))

    assert warehouse.query_rows(
        "SELECT event_key, platform, source_stream FROM base.screen_time_event ORDER BY event_key"
    ) == [("event", "ios", "App.InFocus"), ("mac-event", "macos", "app-usage")]


def test_screen_time_column_removal_preserves_history_and_current_copy_state(warehouse, tmp_path):
    for path in DEFAULT_MIGRATIONS.glob("00[123]_*.sql"):
        shutil.copyfile(path, tmp_path / path.name)
    warehouse.migrate(tmp_path)
    at = _raw().observed_at
    warehouse.connection.execute(
        "INSERT INTO ops.screen_time_segment VALUES ('device','app-in-focus','segment',?, 'raw')",
        [at],
    )
    warehouse.connection.execute(
        "INSERT INTO ops.screen_time_record VALUES "
        "('physical','device','app-in-focus','segment',32,128,'event','app',?,'start',"
        "'reason',1,'1.2','123',2,'parser',0,'100')",
        [at],
    )
    warehouse.connection.execute(
        "INSERT INTO base.screen_time_event SELECT *, ? FROM ops.screen_time_resolve(['event'])",
        [at],
    )
    warehouse.connection.execute(
        "INSERT INTO base.screen_time_event SELECT 'inactive', * EXCLUDE (event_key) "
        "FROM base.screen_time_event WHERE event_key='event'"
    )
    warehouse.connection.execute(
        "UPDATE base.screen_time_event SET is_active=false WHERE event_key='inactive'"
    )
    removed = {
        "app_version",
        "app_build",
        "transition_reason",
        "platform_flag",
        "segment_filename",
        "record_offset",
        "record_metadata_offset",
    }
    retained = [
        row[0]
        for row in warehouse.query_rows("DESCRIBE base.screen_time_event")
        if row[0] not in removed
    ]
    projection = ",".join(retained)
    history = warehouse.query_rows(
        f"SELECT {projection} FROM base.screen_time_event ORDER BY event_key"
    )
    copies = warehouse.query_rows("SELECT * FROM ops.screen_time_record")

    warehouse.migrate()

    assert not removed & {row[0] for row in warehouse.query_rows("DESCRIBE base.screen_time_event")}
    assert (
        warehouse.query_rows(f"SELECT {projection} FROM base.screen_time_event ORDER BY event_key")
        == history
    )
    assert warehouse.query_rows("SELECT * FROM ops.screen_time_record") == copies
    assert warehouse.query_rows("SELECT event_key FROM base.screen_time_transition") == [("event",)]
    assert not removed & {
        row[0] for row in warehouse.query_rows("DESCRIBE base.screen_time_transition")
    }
    assert warehouse.query_rows("SELECT event_key FROM ops.screen_time_resolve(['event'])") == [
        ("event",)
    ]


def test_owntracks_battery_column_removal_preserves_location_and_provenance(warehouse, tmp_path):
    for path in DEFAULT_MIGRATIONS.glob("00[12345]_*.sql"):
        shutil.copyfile(path, tmp_path / path.name)
    warehouse.migrate(tmp_path)
    at = _raw().observed_at
    warehouse.connection.execute(
        "INSERT INTO base.owntracks_location VALUES "
        "('event','device',?,35,139,7,12,80,'raw',1,?,?,'parser')",
        [at, at, at],
    )
    before = warehouse.query_rows("SELECT * EXCLUDE (battery_percent) FROM base.owntracks_location")

    warehouse.migrate()

    assert "battery_percent" not in {
        row[0] for row in warehouse.query_rows("DESCRIBE base.owntracks_location")
    }
    assert (
        warehouse.query_rows("SELECT * EXCLUDE (motionactivities) FROM base.owntracks_location")
        == before
    )


def test_owntracks_motionactivities_migration_preserves_existing_locations(warehouse, tmp_path):
    for path in DEFAULT_MIGRATIONS.glob("*.sql"):
        if path.name < "009_":
            shutil.copyfile(path, tmp_path / path.name)
    warehouse.migrate(tmp_path)
    at = _raw().observed_at
    warehouse.connection.execute(
        "INSERT INTO base.owntracks_location VALUES "
        "('event','device',?,35,139,7,12,'raw',1,?,?,'owntracks-location-v1')",
        [at, at, at],
    )
    before = warehouse.query_rows("SELECT * FROM base.owntracks_location")

    warehouse.migrate()
    warehouse.migrate()

    assert (
        warehouse.query_rows("SELECT * EXCLUDE (motionactivities) FROM base.owntracks_location")
        == before
    )
    assert warehouse.query_rows("SELECT motionactivities FROM base.owntracks_location") == [(None,)]


def test_audit_cleanup_preserves_operational_and_collector_state(warehouse, tmp_path):
    for migration in DEFAULT_MIGRATIONS.glob("*.sql"):
        if migration.name < "007_":
            shutil.copyfile(migration, tmp_path / migration.name)
    warehouse.migrate(tmp_path)
    raw = _raw()
    warehouse.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
    warehouse.begin_job("daily-reconciliation", "daily-run")
    warehouse.finish_job(
        "daily-run", succeeded=True, details={"last_completed_target_date": "2026-10-23"}
    )
    for monitor in ("daily", "screen_time_reconciliation", "screen_time_app_usage_reconciliation"):
        warehouse.publish_heartbeat(monitor, "previous", {"scan_completed_at": "2026-10-24"})
    warehouse.connection.execute(
        "INSERT INTO ops.reconciliation_run (run_id,status,started_at) "
        "VALUES ('old-audit','succeeded',current_timestamp)"
    )
    tables = ("ops.ingestion_metadata", "ops.job_run", "ops.job_lock", "base.screen_time_event")
    before = {name: warehouse.query_rows(f"SELECT * FROM {name}") for name in tables}
    collectors = warehouse.query_rows(
        "SELECT * FROM ops.heartbeat WHERE monitor_name <> 'daily' ORDER BY monitor_name"
    )

    shutil.copyfile(
        DEFAULT_MIGRATIONS / "007_operational_state.sql",
        tmp_path / "007_operational_state.sql",
    )
    warehouse.migrate(tmp_path)

    assert {name: warehouse.query_rows(f"SELECT * FROM {name}") for name in tables} == before
    assert warehouse.query_rows("SELECT * FROM ops.heartbeat ORDER BY monitor_name") == collectors
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='ops' "
            "AND table_name='reconciliation_run'"
        )
        == 0
    )
    warehouse.migrate(tmp_path)
    assert warehouse.query_rows("SELECT * FROM ops.heartbeat ORDER BY monitor_name") == collectors


def test_terminal_receipt_migration_preserves_success_and_makes_interrupted_load_retryable(
    warehouse, tmp_path
):
    for migration in DEFAULT_MIGRATIONS.glob("*.sql"):
        if migration.name < "008_":
            shutil.copyfile(migration, tmp_path / migration.name)
    warehouse.migrate(tmp_path)
    raw = _raw()
    warehouse.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
    success = warehouse.query_rows(
        "SELECT * EXCLUDE (retention_expired_at,retention_started_at) FROM ops.ingestion_metadata"
    )
    data = warehouse.query_rows("SELECT * FROM base.screen_time_event")
    interrupted = replace(raw, key="raw/interrupted", logical_key="interrupted")
    warehouse.mark_failed(interrupted, byte_size=0, error=ValueError("interrupted"))
    warehouse.connection.execute(
        "UPDATE ops.ingestion_metadata SET status='loading', completed_at=NULL, "
        "error_type=NULL, error_message=NULL WHERE object_key=?",
        [interrupted.key],
    )

    shutil.copyfile(
        DEFAULT_MIGRATIONS / "008_terminal_ingestion_receipts.sql",
        tmp_path / "008_terminal_ingestion_receipts.sql",
    )
    warehouse.migrate(tmp_path)

    assert (
        warehouse.query_rows("SELECT * FROM ops.ingestion_metadata WHERE object_key=?", [raw.key])
        == success
    )
    assert warehouse.query_rows("SELECT * FROM base.screen_time_event") == data
    assert warehouse.query_rows(
        "SELECT status,error_type FROM ops.ingestion_metadata WHERE object_key=?", [interrupted.key]
    ) == [("failed", "InterruptedIngestion")]
    assert warehouse.succeeded_keys_for([interrupted]) == set()
    warehouse.load_object(interrupted, byte_size=10, batch=ScreenTimeBatch([_record(interrupted)]))
    assert warehouse.succeeded_keys_for([interrupted]) == {interrupted.key}
    with pytest.raises(duckdb.ConstraintException):
        warehouse.connection.execute(
            "UPDATE ops.ingestion_metadata SET status='loading' WHERE object_key=?", [raw.key]
        )


@pytest.mark.parametrize("expired_status", ["succeeded", "failed"])
def test_retention_cleanup_preserves_active_receipts_and_analytical_history(
    warehouse, tmp_path, expired_status
):
    for migration in DEFAULT_MIGRATIONS.glob("*.sql"):
        if migration.name < "008_":
            shutil.copyfile(migration, tmp_path / migration.name)
    warehouse.migrate(tmp_path)
    raw = _raw()
    expired = replace(raw, key="raw/expired", logical_key="expired")
    failed = replace(raw, key="raw/failed", logical_key="failed")
    warehouse.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
    warehouse.load_object(
        expired,
        byte_size=20,
        batch=ScreenTimeBatch([replace(_record(expired), event_key="old-event")]),
    )
    warehouse.mark_failed(failed, byte_size=0, error=ValueError("retry me"))
    warehouse.connection.execute(
        "UPDATE ops.ingestion_metadata SET status=?, retention_expired_at=current_timestamp "
        "WHERE object_key=?",
        [expired_status, expired.key],
    )
    warehouse.connection.execute(
        "UPDATE ops.ingestion_metadata SET retention_started_at=observed_at"
    )
    active = warehouse.query_rows(
        "SELECT * EXCLUDE (retention_expired_at,retention_started_at) "
        "FROM ops.ingestion_metadata WHERE retention_expired_at IS NULL ORDER BY object_key"
    )
    tables = ("base.screen_time_event", "ops.screen_time_record", "ops.screen_time_segment")
    before = {table: warehouse.query_rows(f"SELECT * FROM {table} ORDER BY 1") for table in tables}

    warehouse.migrate()

    assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 2
    assert (
        warehouse.query_rows("SELECT * FROM ops.ingestion_metadata ORDER BY object_key") == active
    )
    assert {
        table: warehouse.query_rows(f"SELECT * FROM {table} ORDER BY 1") for table in tables
    } == before
    assert warehouse.succeeded_keys_for([raw, expired, failed]) == {raw.key}
    recreated = replace(expired, storage_generation=2)
    warehouse.load_object(
        recreated,
        byte_size=20,
        batch=ScreenTimeBatch([replace(_record(recreated), event_key="old-event")]),
    )
    assert warehouse.succeeded_keys_for([recreated]) == {expired.key}
    assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 2
    warehouse.migrate()
    assert warehouse.succeeded_keys_for([raw, recreated]) == {raw.key, expired.key}


def test_pending_migration_preserves_active_writer_and_retries_after_release(warehouse, tmp_path):
    for migration in DEFAULT_MIGRATIONS.glob("*.sql"):
        if migration.name < "008_":
            shutil.copyfile(migration, tmp_path / migration.name)
    warehouse.migrate(tmp_path)
    warehouse.acquire_job_lock("loader", "collector", lease_seconds=7500)
    lease = warehouse.query_rows("SELECT * FROM ops.job_lock")
    ledger = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")

    with pytest.raises(RuntimeError, match="writer lease"):
        warehouse.migrate()

    assert warehouse.query_rows("SELECT * FROM ops.job_lock") == lease
    assert (
        warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id") == ledger
    )
    warehouse.release_job_lock("loader", "collector")
    warehouse.migrate()
    assert warehouse.query_rows("SELECT * FROM ops.job_lock") == []
    assert "retention_expired_at" not in {
        row[0] for row in warehouse.query_rows("DESCRIBE ops.ingestion_metadata")
    }


@pytest.mark.parametrize("stage", ["copy", "commit"])
def test_migration_excludes_new_writers_until_ledger_replacement_commits(tmp_path, stage):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for migration in DEFAULT_MIGRATIONS.glob("*.sql"):
        if migration.name < "008_":
            shutil.copyfile(migration, legacy / migration.name)
    database = str(tmp_path / "concurrent.duckdb")
    warehouse = Warehouse(connect(WarehouseConfig(database)))
    warehouse.migrate(legacy)
    writer = Warehouse(connect(WarehouseConfig(database)))
    connection = warehouse.connection
    attempted = []
    raw = _raw()

    class ConcurrentWriter:
        copied = False

        def execute(self, sql, *args):
            copy = "CREATE TABLE ops.ingestion_metadata_next" in sql
            if copy:
                self.copied = True
            if not attempted and (
                (stage == "copy" and copy)
                or (stage == "commit" and self.copied and sql == "COMMIT")
            ):
                acquired = writer.acquire_job_lock("loader", "worker", lease_seconds=7500)
                attempted.append(acquired)
                if acquired:
                    writer.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
                    writer.release_job_lock("loader", "worker")
            return connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(connection, name)

    warehouse.connection = ConcurrentWriter()
    try:
        warehouse.migrate()
        assert attempted == [False]
        assert writer.acquire_job_lock("loader", "worker", lease_seconds=7500)
        writer.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
        writer.release_job_lock("loader", "worker")
        assert writer.succeeded_keys_for([raw]) == {raw.key}
        assert writer.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    finally:
        warehouse.close()
        writer.close()


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_migration_commit_stops_writes_until_reconnected(tmp_path, committed):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for migration in DEFAULT_MIGRATIONS.glob("*.sql"):
        if migration.name < "008_":
            shutil.copyfile(migration, legacy / migration.name)
    database = str(tmp_path / "uncertain.duckdb")
    warehouse = Warehouse(connect(WarehouseConfig(database)))
    warehouse.migrate(legacy)
    raw = _raw()
    warehouse.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
    connection = warehouse.connection
    after_failure = []

    class UncertainCommit:
        copying = False
        failed = False

        def execute(self, sql, *args):
            if self.failed:
                after_failure.append(sql)
            if "CREATE TABLE ops.ingestion_metadata_next" in sql:
                self.copying = True
            if self.copying and sql == "COMMIT":
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
            warehouse.migrate()
        assert warehouse.connection_usable is False
        assert after_failure == []
    finally:
        warehouse.close()
    warehouse = Warehouse(connect(WarehouseConfig(database)))
    try:
        assert warehouse.query_value(
            "SELECT count(*) FROM ops.schema_migration WHERE migration_id='008_terminal_ingestion_receipts.sql'"
        ) == int(committed)
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_event") == 1
        assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 1
        warehouse.connection.execute(
            "UPDATE ops.job_lock SET expires_at=current_timestamp - INTERVAL '1 second'"
        )
        warehouse.migrate()
        assert warehouse.succeeded_keys_for([raw]) == {raw.key}
        assert warehouse.acquire_job_lock("loader", "retry", lease_seconds=120)
        assert warehouse.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)])) == 0
        warehouse.release_job_lock("loader", "retry")
        assert warehouse.query_rows("SELECT * FROM ops.job_lock") == []
    finally:
        warehouse.close()
