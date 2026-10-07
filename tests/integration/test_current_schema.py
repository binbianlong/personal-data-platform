from __future__ import annotations

import duckdb
import pytest

from personal_data_platform.cli import main
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


@pytest.fixture
def warehouse():
    value = Warehouse(connect(WarehouseConfig(":memory:")))
    yield value
    value.close()


def test_current_schema_has_one_receipt_and_no_profile(warehouse):
    warehouse.migrate()
    assert warehouse.query_rows("SELECT migration_id FROM ops.schema_migration") == [
        ("001_current.sql",)
    ]
    assert [row[0] for row in warehouse.query_rows("DESCRIBE ops.schema_migration")] == [
        "migration_id",
        "checksum",
        "applied_at",
    ]
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='ops' "
            "AND table_name IN ('fitbit_notification','fitbit_raw_intent','fitbit_scope')"
        )
        == 0
    )


@pytest.mark.parametrize("profile", [None, "legacy", "west"])
def test_old_history_is_rejected_without_modifying_it(warehouse, profile):
    warehouse.connection.execute(
        "CREATE SCHEMA ops; CREATE TABLE ops.schema_migration "
        "(migration_id VARCHAR, checksum VARCHAR, applied_at TIMESTAMPTZ)"
    )
    warehouse.connection.execute(
        "INSERT INTO ops.schema_migration VALUES ('001_initial.sql', 'preserved', now())"
    )
    if profile:
        warehouse.connection.execute("ALTER TABLE ops.schema_migration ADD schema_profile VARCHAR")
        warehouse.connection.execute("UPDATE ops.schema_migration SET schema_profile=?", [profile])
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


def test_fresh_target_ignores_an_attached_old_database(warehouse):
    warehouse.connection.execute("ATTACH ':memory:' AS unrelated")
    warehouse.connection.execute(
        "CREATE SCHEMA unrelated.ops; CREATE TABLE unrelated.ops.schema_migration "
        "(migration_id VARCHAR, checksum VARCHAR, applied_at TIMESTAMPTZ, schema_profile VARCHAR)"
    )
    warehouse.connection.execute(
        "INSERT INTO unrelated.ops.schema_migration VALUES ('old.sql','preserved',now(),'west')"
    )
    before = warehouse.query_rows("SELECT * FROM unrelated.ops.schema_migration")
    warehouse.migrate()
    warehouse.migrate()
    assert warehouse.query_value("SELECT count(*) FROM ops.schema_migration") == 1
    assert warehouse.query_rows("SELECT * FROM unrelated.ops.schema_migration") == before


def test_platform_migrate_initializes_a_local_database(tmp_path):
    database = tmp_path / "local.duckdb"
    assert main(["migrate", "--database", str(database)]) == 0
    with duckdb.connect(str(database)) as connection:
        assert connection.execute("SELECT migration_id FROM ops.schema_migration").fetchall() == [
            ("001_current.sql",)
        ]


def test_platform_migrate_rejects_a_nonlocal_database(capsys):
    assert main(["migrate", "--database", "md:production"]) == 1
    assert "local .duckdb or .db" in capsys.readouterr().err
