from __future__ import annotations

import shutil
from unittest.mock import Mock

import duckdb
import pytest

from personal_data_platform.cli import main
from personal_data_platform.storage.migrations import DEFAULT_MIGRATIONS
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from personal_data_platform.storage.warehouse import WarehouseConnectionError


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


def _insert_event(warehouse):
    warehouse.connection.execute(
        "INSERT INTO base.iphone_screen_time_event "
        "(event_key,device_key,bundle_id,event_at,state,is_valid) "
        "VALUES ('event','phone','app',now(),'start',TRUE)"
    )


def _ledger(warehouse):
    if "ops.schema_migration" not in warehouse.relation_names():
        return []
    return warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")


def test_concurrent_startup_preserves_data_committed_by_other_connection(tmp_path):
    database = str(tmp_path / "concurrent.duckdb")
    connection = connect(WarehouseConfig(database))
    other = Warehouse(connect(WarehouseConfig(database)))
    committed = []

    class InterveningConnection:
        def execute(self, sql, *parameters):
            if sql == "CREATE SCHEMA IF NOT EXISTS ops" and not committed:
                other.migrate()
                _insert_event(other)
                committed.extend(_ledger(other))
            return connection.execute(sql, *parameters)

        def close(self):
            connection.close()

    value = Warehouse(InterveningConnection())
    try:
        value.migrate()
        assert _ledger(value) == committed
        assert value.query_rows("SELECT event_key FROM base.iphone_screen_time_event") == [
            ("event",)
        ]
    finally:
        value.close()
        other.close()


def test_repeated_migration_preserves_loaded_data_and_ledger(warehouse):
    warehouse.migrate()
    _insert_event(warehouse)
    before = _ledger(warehouse)
    events = warehouse.query_rows("SELECT * FROM base.iphone_screen_time_event")
    warehouse.migrate()
    assert _ledger(warehouse) == before
    assert warehouse.query_rows("SELECT * FROM base.iphone_screen_time_event") == events


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
def test_invalid_applied_history_is_rejected_without_changes(warehouse, column, value, message):
    warehouse.migrate()
    warehouse.connection.execute(f"UPDATE ops.schema_migration SET {column}=?", [value])
    before = _ledger(warehouse)
    with pytest.raises(RuntimeError, match=message):
        warehouse.migrate()
    assert _ledger(warehouse) == before


def test_applied_sql_changes_stop_before_applying_later_migrations(warehouse, pending_migrations):
    warehouse.migrate()
    before = _ledger(warehouse)
    initial = next(path for path in pending_migrations.glob("*.sql") if path.name.startswith("001"))
    initial.write_text(initial.read_text() + "\nSELECT 1;\n")
    with pytest.raises(RuntimeError, match="applied migration changed"):
        warehouse.migrate(pending_migrations)
    assert _ledger(warehouse) == before
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name='migration_probe'"
        )
        == 0
    )


@pytest.mark.parametrize("initial", [True, False], ids=["initial", "forward"])
def test_failed_migration_rolls_back_schema_data_and_ledger(warehouse, tmp_path, initial):
    if initial:
        source = next(DEFAULT_MIGRATIONS.glob("001*.sql"))
        path = tmp_path / source.name
        sql = source.read_text()
        before = []
        table = "sleep_session"
    else:
        warehouse.migrate()
        before = _ledger(warehouse)
        for source in DEFAULT_MIGRATIONS.glob("*.sql"):
            shutil.copyfile(source, tmp_path / source.name)
        path = tmp_path / "999_next.sql"
        sql = "CREATE TABLE base.next (id INTEGER); INSERT INTO base.next VALUES (1);"
        table = "next"
    path.write_text(sql + "\nSELECT error('interrupted migration');")
    with pytest.raises(duckdb.InvalidInputException, match="interrupted migration"):
        warehouse.migrate(tmp_path)
    assert _ledger(warehouse) == before
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM duckdb_tables() WHERE schema_name='base' AND table_name=?",
            [table],
        )
        == 0
    )
    path.write_text(sql)
    warehouse.migrate(tmp_path)
    assert len(_ledger(warehouse)) == len(before) + 1
    if not initial:
        assert warehouse.query_rows("SELECT * FROM base.next") == [(1,)]
    ledger = _ledger(warehouse)
    warehouse.migrate(tmp_path)
    assert _ledger(warehouse) == ledger


def test_pending_migration_preserves_active_writer_and_retries_after_release(
    warehouse, pending_migrations
):
    warehouse.migrate()
    warehouse.acquire_job_lock("loader", "collector", lease_seconds=7500)
    lease = warehouse.query_rows("SELECT * FROM ops.job_lock")
    before = _ledger(warehouse)
    with pytest.raises(RuntimeError, match="writer lease"):
        warehouse.migrate(pending_migrations)
    assert warehouse.query_rows("SELECT * FROM ops.job_lock") == lease
    assert _ledger(warehouse) == before
    warehouse.release_job_lock("loader", "collector")
    warehouse.migrate(pending_migrations)
    assert warehouse.query_rows("SELECT * FROM ops.job_lock") == []
    assert warehouse.query_rows("SELECT * FROM base.migration_probe") == [(1,)]


@pytest.mark.parametrize("stage", ["apply", "commit"])
def test_migration_excludes_new_writers_until_commit(tmp_path, pending_migrations, stage):
    database = str(tmp_path / "concurrent.duckdb")
    value = Warehouse(connect(WarehouseConfig(database)))
    value.migrate()
    writer = Warehouse(connect(WarehouseConfig(database)))
    connection = value.connection
    attempted = []

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
                attempted.append(writer.acquire_job_lock("loader", "worker", lease_seconds=7500))
            return connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(connection, name)

    value.connection = ConcurrentWriter()
    try:
        value.migrate(pending_migrations)
        assert attempted == [False]
        assert writer.acquire_job_lock("loader", "worker", lease_seconds=7500)
        _insert_event(writer)
        writer.release_job_lock("loader", "worker")
        assert writer.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 1
    finally:
        value.close()
        writer.close()


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_stops_connection_use_until_reconnected(
    tmp_path, pending_migrations, committed
):
    database = str(tmp_path / "uncertain.duckdb")
    value = Warehouse(connect(WarehouseConfig(database)))
    value.migrate()
    _insert_event(value)
    connection = value.connection
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

    value.connection = UncertainCommit()
    try:
        with pytest.raises(WarehouseConnectionError, match="commit outcome unknown"):
            value.migrate(pending_migrations)
        assert value.connection_usable is False
        assert after_failure == []
    finally:
        value.close()
    value = Warehouse(connect(WarehouseConfig(database)))
    try:
        assert value.query_value(
            "SELECT count(*) FROM ops.schema_migration WHERE migration_id='999_pending.sql'"
        ) == int(committed)
        assert value.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 1
        assert value.query_value("SELECT count(*) FROM ops.job_lock") == 1
        value.connection.execute("UPDATE ops.job_lock SET expires_at=now()-INTERVAL '1 second'")
        value.migrate(pending_migrations)
        assert value.acquire_job_lock("loader", "retry", lease_seconds=120)
        value.release_job_lock("loader", "retry")
        assert value.query_rows("SELECT * FROM ops.job_lock") == []
    finally:
        value.close()


def test_unexpected_history_columns_are_rejected_without_modification(warehouse):
    warehouse.connection.execute(
        "CREATE SCHEMA ops; CREATE TABLE ops.schema_migration "
        "(migration_id VARCHAR,checksum VARCHAR,applied_at TIMESTAMPTZ,extra VARCHAR); "
        "INSERT INTO ops.schema_migration VALUES ('001_initial.sql','old',now(),'preserve')"
    )
    before = _ledger(warehouse)
    with pytest.raises(RuntimeError, match="rebuild.*empty"):
        warehouse.migrate()
    assert _ledger(warehouse) == before


@pytest.mark.parametrize("schema", ["base", "ops", "marts"])
@pytest.mark.parametrize("empty_ledger", [False, True])
def test_untracked_existing_relations_require_empty_target(warehouse, schema, empty_ledger):
    warehouse.connection.execute(
        f"CREATE SCHEMA {schema}; CREATE TABLE {schema}.previous (id INTEGER)"
    )
    if empty_ledger:
        warehouse.connection.execute(
            "CREATE SCHEMA IF NOT EXISTS ops; CREATE TABLE ops.schema_migration "
            "(migration_id VARCHAR,checksum VARCHAR,applied_at TIMESTAMPTZ)"
        )
    with pytest.raises(RuntimeError, match="rebuild.*empty"):
        warehouse.migrate()
    assert warehouse.query_value(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name='schema_migration'"
    ) == int(empty_ledger)


def test_legacy_history_is_rejected_without_touching_data(warehouse):
    warehouse.connection.execute(
        "CREATE SCHEMA ops; CREATE TABLE ops.schema_migration "
        "(migration_id VARCHAR,checksum VARCHAR,applied_at TIMESTAMPTZ); "
        "INSERT INTO ops.schema_migration VALUES ('012_split_screen_time_sources.sql','old',now()); "
        "CREATE SCHEMA base; CREATE TABLE base.legacy (id INTEGER); "
        "INSERT INTO base.legacy VALUES (42)"
    )
    before = _ledger(warehouse)
    with pytest.raises(RuntimeError, match="rebuild.*empty"):
        warehouse.migrate()
    assert _ledger(warehouse) == before
    assert warehouse.query_rows("SELECT * FROM base.legacy") == [(42,)]


def test_fresh_target_ignores_an_attached_database(warehouse):
    warehouse.connection.execute(
        "ATTACH ':memory:' AS unrelated; CREATE SCHEMA unrelated.ops; "
        "CREATE TABLE unrelated.ops.schema_migration "
        "(migration_id VARCHAR,checksum VARCHAR,applied_at TIMESTAMPTZ,extra VARCHAR); "
        "INSERT INTO unrelated.ops.schema_migration VALUES ('other.sql','old',now(),'preserve')"
    )
    before = warehouse.query_rows("SELECT * FROM unrelated.ops.schema_migration")
    warehouse.migrate()
    warehouse.migrate()
    assert len(_ledger(warehouse)) == 1
    assert warehouse.query_rows("SELECT * FROM unrelated.ops.schema_migration") == before


def test_platform_migrate_initializes_a_local_database(tmp_path):
    database = tmp_path / "local.duckdb"
    assert main(["migrate", "--database", str(database)]) == 0
    with duckdb.connect(str(database)) as connection:
        assert connection.execute("SELECT count(*) FROM ops.schema_migration").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM base.sleep_session").fetchone() == (0,)


def test_platform_migrate_rejects_nonlocal_database(capsys):
    assert main(["migrate", "--database", "md:production"]) == 1
    assert "local .duckdb or .db" in capsys.readouterr().err
