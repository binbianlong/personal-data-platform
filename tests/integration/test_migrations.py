from __future__ import annotations

import shutil
from dataclasses import replace

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


def test_applied_sql_changes_stop_before_applying_later_migrations(warehouse, tmp_path):
    migrations = DEFAULT_MIGRATIONS
    for path in migrations.glob("*.sql"):
        shutil.copyfile(path, tmp_path / path.name)
    warehouse.migrate(tmp_path)
    before = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")
    initial = tmp_path / "001_current.sql"
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
        path = tmp_path / "001_current.sql"
        sql = (DEFAULT_MIGRATIONS / path.name).read_text()
        before = []
    else:
        warehouse.migrate()
        before = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")
        shutil.copyfile(DEFAULT_MIGRATIONS / "001_current.sql", tmp_path / "001_current.sql")
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


def test_current_schema_sets_iphone_and_mac_platform(warehouse):
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
