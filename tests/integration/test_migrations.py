from __future__ import annotations

import shutil

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


def test_initial_schema_supports_current_ingestion_without_archives(warehouse):
    warehouse.migrate()
    assert warehouse.query_rows("SELECT migration_id FROM ops.schema_migration") == [
        ("001_initial.sql",)
    ]
    assert warehouse.query_rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'base' "
        "ORDER BY table_name"
    ) == [("screen_time_event",), ("screen_time_transition",)]
    columns = dict(
        warehouse.query_rows(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'ops' AND table_name = 'ingestion_metadata'"
        )
    )
    assert "device_key" not in columns
    assert "segment_key" not in columns
    for name in ("source_id", "schema_version", "subject_key", "logical_key"):
        assert columns[name] == "NO"
    assert warehouse.query_rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'ops' "
        "ORDER BY table_name"
    ) == [
        (name,)
        for name in (
            "heartbeat",
            "ingestion_metadata",
            "job_lock",
            "job_run",
            "reconciliation_run",
            "schema_migration",
            "screen_time_deletion_match",
            "screen_time_record",
            "screen_time_segment",
            "screen_time_tombstone",
        )
    ]
    assert warehouse.query_rows(
        "SELECT column_name, column_default FROM information_schema.columns "
        "WHERE table_schema = 'ops' AND table_name = 'ingestion_metadata' "
        "AND column_name IN ('source_id', 'schema_version') ORDER BY column_name"
    ) == [("schema_version", None), ("source_id", None)]
    raw = _raw()
    warehouse.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
    assert warehouse.query_rows("SELECT event_key FROM base.screen_time_transition") == [("event",)]
    assert warehouse.query_rows("SELECT source_segment_names FROM ops.screen_time_segment") == [
        ([],)
    ]
    with pytest.raises(duckdb.ConstraintException):
        warehouse.connection.execute(
            "UPDATE ops.screen_time_segment SET source_segment_names = NULL"
        )


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


def test_applied_sql_changes_stop_before_applying_later_migrations(warehouse, tmp_path):
    for path in DEFAULT_MIGRATIONS.glob("*.sql"):
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
        path = tmp_path / "002_next.sql"
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
