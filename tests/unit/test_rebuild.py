from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.recovery.rebuild import (
    rebuild_inventory,
    require_empty_rebuild_target,
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
