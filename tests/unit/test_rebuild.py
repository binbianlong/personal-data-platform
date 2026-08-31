from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.recovery.rebuild import (
    rebuild_inventory,
    require_empty_rebuild_target,
    run_rebuild,
    run_rebuild_from_env,
    validate_rebuild_target,
)
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


def test_rebuild_inventory_is_order_independent() -> None:
    first = datetime(2026, 8, 26, tzinfo=UTC)
    observations = [
        SimpleNamespace(
            key="later",
            device_key="a",
            stream="app-in-focus",
            segment_key="one",
            observed_at=first + timedelta(days=1),
        ),
        SimpleNamespace(
            key="first",
            device_key="a",
            stream="app-in-focus",
            segment_key="one",
            observed_at=first,
        ),
        SimpleNamespace(
            key="second-device",
            device_key="b",
            stream="app-in-focus",
            segment_key="two",
            observed_at=first,
        ),
    ]

    inventory = rebuild_inventory(observations)

    assert inventory["raw_object_count"] == 3
    assert inventory["device_count"] == 2
    assert inventory["segment_count"] == 2
    assert inventory["first_observed_at"] == first.isoformat()


def test_rebuild_refuses_production_and_unsafe_names() -> None:
    with pytest.raises(ValueError, match="production"):
        validate_rebuild_target("personal_data", "personal_data")
    with pytest.raises(ValueError, match="only"):
        validate_rebuild_target("md:other?token=secret", "personal_data")

    validate_rebuild_target("personal-data-rebuild-20260827", "personal_data")


def test_rebuild_requires_an_empty_database(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "scratch.duckdb"))))
    try:
        require_empty_rebuild_target(warehouse)
        warehouse.connection.execute("CREATE TABLE existing (value INTEGER)")
        with pytest.raises(ValueError, match="existing"):
            require_empty_rebuild_target(warehouse)
    finally:
        warehouse.close()


def test_rebuild_ignores_tables_in_other_attached_databases() -> None:
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.connection.execute("ATTACH ':memory:' AS production")
        warehouse.connection.execute("CREATE TABLE production.main.existing (value INTEGER)")

        require_empty_rebuild_target(warehouse)

        warehouse.connection.execute("CREATE VIEW existing AS SELECT 1 AS value")
        with pytest.raises(ValueError, match="existing"):
            require_empty_rebuild_target(warehouse)
    finally:
        warehouse.close()


def test_rebuild_dry_run_uses_canonical_raw_prefix(monkeypatch) -> None:
    prefixes = []

    class Repository:
        def list_raw(self, prefix):
            prefixes.append(prefix)
            return []

    monkeypatch.setenv("B2_RAW_PREFIX", "test/not-screen-time/")
    monkeypatch.setattr(
        "personal_data_platform.storage.b2.B2RawRepository.from_env", lambda: Repository()
    )

    assert run_rebuild_from_env(dry_run=True, target_db=None) == 0
    assert prefixes == ["raw/screen_time/v1/"]


@pytest.mark.parametrize("dbt_fails", [False, True])
def test_rebuild_uses_scratch_credentials_and_restores_environment(monkeypatch, dbt_fails) -> None:
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "ambient_database")
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "ambient_token")
    connections = []
    dbt_calls = []

    def connect_scratch(config):
        connections.append(config)
        return connect(WarehouseConfig(":memory:"))

    def transform(*, target):
        dbt_calls.append(target)
        assert os.environ["MOTHERDUCK_DATABASE"] == "scratch_database"
        assert os.environ["MOTHERDUCK_TOKEN"] == "scratch_token"
        if dbt_fails:
            raise RuntimeError("dbt failed")

    monkeypatch.setattr("personal_data_platform.recovery.rebuild.connect", connect_scratch)
    monkeypatch.setattr("personal_data_platform.recovery.rebuild.run_dbt", transform)

    def rebuild():
        return run_rebuild(
            SimpleNamespace(list_raw=lambda prefix: []),
            target_db="scratch_database",
            token="scratch_token",
            production_db="production_database",
        )

    if dbt_fails:
        with pytest.raises(RuntimeError, match="dbt failed"):
            rebuild()
    else:
        assert rebuild() == 0

    assert connections == [WarehouseConfig(database="scratch_database", token="scratch_token")]
    assert dbt_calls == ["prod"]
    assert os.environ["MOTHERDUCK_DATABASE"] == "ambient_database"
    assert os.environ["MOTHERDUCK_TOKEN"] == "ambient_token"
