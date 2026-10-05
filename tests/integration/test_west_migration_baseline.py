from __future__ import annotations

import shutil

import pytest

from personal_data_platform.config import ConfigurationError
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
    yield value
    value.close()


def test_west_baseline_creates_only_final_fitbit_schema(warehouse):
    warehouse.migrate(profile="west")
    tables = {
        row[0]
        for row in warehouse.query_rows(
            "SELECT table_schema || '.' || table_name FROM information_schema.tables"
        )
    }
    assert {
        "base.fitbit_steps",
        "base.fitbit_resting_heart_rate",
        "base.fitbit_active_zone",
        "base.fitbit_sleep",
        "base.fitbit_sleep_stage",
        "base.fitbit_sleep_wake",
        "base.fitbit_heart_rate_minute",
        "ops.fitbit_minute_coverage",
        "ops.fitbit_scope",
        "ops.fitbit_notification",
        "ops.fitbit_notification_scope",
        "ops.fitbit_attempt",
        "ops.fitbit_bundle",
        "ops.fitbit_bundle_attempt",
        "ops.fitbit_bundle_chunk",
        "ops.fitbit_repair_cursor",
        "ops.fitbit_scope_success",
        "ops.fitbit_device_sync",
    } <= tables
    assert "base.fitbit_heart_rate" not in tables
    assert "ops.fitbit_raw_intent" not in tables
    assert warehouse.query_rows(
        "SELECT migration_id FROM ops.schema_migration ORDER BY migration_id"
    ) == [
        ("001_initial.sql",),
        ("002_screen_time_app_usage_platform.sql",),
        ("003_fitbit_baseline.sql",),
        ("004_raw_retention_origin.sql",),
    ]
    assert warehouse.query_rows(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='ops' "
        "AND table_name='ingestion_metadata' AND column_name='retention_started_at'"
    ) == [("retention_started_at",)]


@pytest.mark.parametrize("direction", ["legacy-to-west", "west-to-legacy"])
def test_west_baseline_rejects_legacy_database(warehouse, direction):
    initial, other = ("legacy", "west") if direction == "legacy-to-west" else ("west", "legacy")
    warehouse.migrate(profile=initial)
    before = warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id")
    with pytest.raises(RuntimeError, match="profile"):
        warehouse.migrate(profile=other)
    assert (
        warehouse.query_rows("SELECT * FROM ops.schema_migration ORDER BY migration_id") == before
    )


def test_west_migration_reapply_preserves_screen_time_and_fitbit(warehouse):
    warehouse.migrate(profile="west")
    raw = _raw()
    warehouse.load_object(raw, byte_size=10, batch=ScreenTimeBatch([_record(raw)]))
    warehouse.connection.execute("INSERT INTO ops.fitbit_notification VALUES ('n', 's', now())")
    tables = (
        "ops.schema_migration",
        "ops.ingestion_metadata",
        "base.screen_time_event",
        "ops.fitbit_notification",
    )
    before = {table: warehouse.query_rows(f"SELECT * FROM {table}") for table in tables}
    warehouse.migrate(profile="west")
    assert {table: warehouse.query_rows(f"SELECT * FROM {table}") for table in tables} == before


def test_west_baseline_matches_forward_schema_and_constraints(warehouse):
    warehouse.migrate(profile="west")
    legacy = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        legacy.migrate()
        sql = (
            "SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema IN ('base','ops') "
            "AND table_name NOT IN ('fitbit_heart_rate', 'fitbit_raw_intent') "
            "ORDER BY table_schema, table_name, ordinal_position"
        )
        assert warehouse.query_rows(sql) == legacy.query_rows(sql)
        sql = (
            "SELECT schema_name, table_name, constraint_type, constraint_text FROM duckdb_constraints() "
            "WHERE schema_name IN ('base','ops') "
            "AND table_name NOT IN ('fitbit_heart_rate','fitbit_raw_intent') "
            "ORDER BY schema_name, table_name, constraint_type, constraint_text"
        )
        assert warehouse.query_rows(sql) == legacy.query_rows(sql)
    finally:
        legacy.close()


def test_interrupted_west_common_migrations_resume_and_reject_legacy(warehouse, tmp_path):
    shutil.copyfile(DEFAULT_MIGRATIONS / "001_initial.sql", tmp_path / "001_initial.sql")
    warehouse.migrate(tmp_path, profile="west")
    with pytest.raises(RuntimeError, match="profile"):
        warehouse.migrate(profile="legacy")
    warehouse.migrate(profile="west")
    assert warehouse.query_value("SELECT count(*) FROM ops.schema_migration") == 4


def test_existing_unprofiled_legacy_receipts_reject_west_without_mutation(warehouse):
    warehouse.connection.execute(
        "CREATE SCHEMA ops; CREATE TABLE ops.schema_migration "
        "(migration_id VARCHAR, checksum VARCHAR, applied_at TIMESTAMPTZ)"
    )
    warehouse.connection.execute(
        "INSERT INTO ops.schema_migration VALUES ('001_initial.sql','old',now())"
    )
    before = warehouse.query_rows("SELECT * FROM ops.schema_migration")
    with pytest.raises(RuntimeError, match="profile"):
        warehouse.migrate(profile="west")
    assert warehouse.query_rows("SELECT * FROM ops.schema_migration") == before
    assert len(warehouse.query_rows("DESCRIBE ops.schema_migration")) == 3


def test_schema_profile_validates_runtime_selection(monkeypatch):
    from personal_data_platform import config

    monkeypatch.delenv("PDP_SCHEMA_PROFILE", raising=False)
    assert config.schema_profile() == "legacy"
    monkeypatch.setenv("PDP_SCHEMA_PROFILE", "west")
    assert config.schema_profile() == "west"
    monkeypatch.setenv("PDP_SCHEMA_PROFILE", "unknown")
    with pytest.raises(ConfigurationError, match="PDP_SCHEMA_PROFILE"):
        config.schema_profile()


def test_common_west_sql_is_byte_identical():
    for name in ("001_initial.sql", "002_screen_time_app_usage_platform.sql"):
        assert (DEFAULT_MIGRATIONS / "west" / name).read_bytes() == (
            DEFAULT_MIGRATIONS / name
        ).read_bytes()


@pytest.mark.parametrize(
    "profile,filename", [("legacy", "003_fitbit_baseline.sql"), ("west", "003_fitbit.sql")]
)
def test_explicit_migration_paths_cannot_bypass_profile_selection(
    warehouse, tmp_path, profile, filename
):
    (tmp_path / filename).write_text("CREATE TABLE main.wrong_profile (id INTEGER);")
    with pytest.raises(RuntimeError, match="profile"):
        warehouse.migrate(tmp_path, profile=profile)
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='wrong_profile'"
        )
        == 0
    )


def test_legacy_ledger_upgrade_preserves_applied_sql_receipts(warehouse):
    warehouse.migrate()
    warehouse.connection.execute("ALTER TABLE ops.schema_migration DROP COLUMN schema_profile")
    before = warehouse.query_rows(
        "SELECT migration_id,checksum,applied_at FROM ops.schema_migration ORDER BY migration_id"
    )
    warehouse.migrate()
    assert (
        warehouse.query_rows(
            "SELECT migration_id,checksum,applied_at FROM ops.schema_migration ORDER BY migration_id"
        )
        == before
    )


def test_preflight_rejects_invalid_schema_profile_before_accessing_clients(monkeypatch):
    from personal_data_platform.preflight import run_preflight_from_env

    monkeypatch.setenv("PDP_SCHEMA_PROFILE", "unknown")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(ConfigurationError, match="PDP_SCHEMA_PROFILE"):
        run_preflight_from_env()
