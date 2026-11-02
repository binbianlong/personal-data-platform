"""Raw logical IDs are the OwnTracks loading and reconciliation identity."""

import gzip
import json
from datetime import UTC, datetime

import pytest

from personal_data_platform.loader.job import run_loader, run_loader_objects
from personal_data_platform.sources.owntracks.adapter import OwnTracksSource
from personal_data_platform.sources.owntracks.raw import prepare_location
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


class Store:
    def __init__(self):
        self.objects = {}
        self.reads = []

    def add(self, **fields):
        key, _, compressed = prepare_location(
            json.dumps(
                {"_type": "location", "tst": 1791504000, "lat": 35, "lon": 139, **fields}
            ).encode(),
            device_key="a" * 64,
        )
        raw = OwnTracksSource().parse_raw_key(
            key, storage_created_at=datetime.now(UTC), storage_generation=12
        )
        self.objects[key] = raw, compressed
        return raw

    def list_raw(self, prefix):
        return [raw for raw, _ in self.objects.values() if raw.key.startswith(prefix)]

    def get_raw(self, key, *, generation):
        raw, compressed = self.objects[key]
        assert raw.storage_generation == generation
        self.reads.append(key)
        return compressed


@pytest.fixture
def warehouse():
    value = Warehouse(connect(WarehouseConfig(":memory:")))
    value.migrate()
    yield value
    value.close()


def test_loader_preserves_same_time_different_content_and_uses_filtered_base_ids(
    warehouse, monkeypatch
):
    store = Store()
    first, second = store.add(), store.add(lat=36)
    source = OwnTracksSource()
    ids = warehouse.location_point_ids
    queried = []

    def filtered(values=None):
        assert values is not None, "event load must not fetch the full location table"
        values = tuple(values)
        queried.append(values)
        return ids(values)

    monkeypatch.setattr(warehouse, "location_point_ids", filtered)
    assert run_loader_objects(store, warehouse, [first], source=source).records == 1
    assert run_loader(store, warehouse, source=source).skipped == 1
    assert run_loader(store, warehouse, source=source).skipped == 2
    assert store.reads == [first.key, second.key]
    assert queried[0] == (first.logical_key,)
    assert warehouse.query_rows(
        "SELECT location_point_id,observed_at,latitude FROM base.location_point ORDER BY latitude"
    ) == [(first.logical_key, first.observed_at, 35), (second.logical_key, second.observed_at, 36)]


def test_reprocess_is_explicit_and_updates_existing_point(warehouse):
    store = Store()
    store.add(motionactivities=["walking"])
    source = OwnTracksSource()
    assert run_loader(store, warehouse, source=source).records == 1
    warehouse.connection.execute("UPDATE base.location_point SET motion_activities=NULL")
    source.parser_version = "corrected"
    assert run_loader(store, warehouse, source=source).skipped == 1
    assert run_loader(store, warehouse, source=source, reprocess=True).records == 1
    assert warehouse.query_rows("SELECT motion_activities FROM base.location_point") == [
        (["walking"],)
    ]


@pytest.mark.parametrize("damage", ["hash", "deflate", "generation"])
def test_corrupt_or_changed_raw_never_creates_base_row(warehouse, damage):
    store = Store()
    raw = store.add()
    if damage == "hash":
        store.objects[raw.key] = raw, gzip.compress(b"{}")
    elif damage == "deflate":
        store.objects[raw.key] = raw, b"\x1f\x8b\x08\x00" + b"broken" * 8
    else:
        from dataclasses import replace

        store.objects[raw.key] = replace(raw, storage_generation=13), store.objects[raw.key][1]
    result = run_loader_objects(store, warehouse, [raw], source=OwnTracksSource())
    assert result.failed == 1
    assert not warehouse.location_point_ids()
    assert not any(
        name.startswith("ops.") and name not in {"ops.job_lock", "ops.schema_migration"}
        for name in warehouse.relation_names()
    )
