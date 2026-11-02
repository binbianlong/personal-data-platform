import gzip
import json
from datetime import UTC, datetime

import pytest

from personal_data_platform.loader.job import run_loader
from personal_data_platform.reconciliation.job import run_reconciliation
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


class RawStore:
    def __init__(self, source):
        self.source = source
        self.objects = {}

    def put_compressed_raw(self, key, compressed):
        self.objects.setdefault(key, compressed)

    def list_raw(self, prefix):
        return [
            self.source.parse_raw_key(
                key, storage_created_at=datetime(2026, 10, 9, tzinfo=UTC), storage_generation=12
            )
            for key in self.objects
            if key.startswith(prefix)
        ]

    def get_raw(self, key, *, generation):
        assert generation == 12
        return self.objects[key]


def location_store(**fields):
    source = get_source("owntracks", "location")
    from personal_data_platform.sources.owntracks.raw import prepare_location

    key, payload, compressed = prepare_location(
        json.dumps(
            {
                "_type": "location",
                "tst": 1791504000,
                "lat": 35,
                "lon": 139,
                "acc": 7,
                "vel": 12,
                "batt": 80,
                "alt": 30,
                "m": 1,
                "t": "u",
                **fields,
            }
        ).encode(),
        device_key="a" * 64,
    )
    store = RawStore(source)
    store.put_compressed_raw(key, compressed)
    return source, store, key, payload


def test_loader_commits_one_location_and_redelivery_is_idempotent():
    source, store, key, _ = location_store()
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate()
        assert not {"altitude_m", "monitoring_mode", "trigger", "battery_percent"} & {
            row[0] for row in warehouse.query_rows("DESCRIBE base.location_point")
        }
        assert run_loader(store, warehouse, source=source).records == 1
        assert run_loader(store, warehouse, source=source).skipped == 1
        assert warehouse.query_rows(
            "SELECT device_key,observed_at,latitude,longitude,accuracy_meters,speed_kmh,"
            "location_point_id "
            "FROM base.location_point"
        ) == [
            (
                "a" * 64,
                datetime(2026, 10, 9, tzinfo=UTC),
                35.0,
                139.0,
                7.0,
                12.0,
                next(iter(store.list_raw(source.raw_prefixes[0]))).logical_key,
            )
        ]
        assert {r for r in warehouse.relation_names() if r.startswith("ops.")} == {
            "ops.job_lock",
            "ops.schema_migration",
        }
        warehouse.migrate()
        assert warehouse.query_value("SELECT count(*) FROM base.location_point") == 1
    finally:
        warehouse.close()


def test_failed_batch_rolls_back_location_and_can_be_retried(monkeypatch):
    source, store, _, _ = location_store()
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    from personal_data_platform.sources.owntracks import motherduck

    write = motherduck.write_batch

    def interrupted(*args, **kwargs):
        write(*args, **kwargs)
        raise RuntimeError("interrupted")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(motherduck, "write_batch", interrupted)
            assert run_loader(store, warehouse, source=source).failed == 1
        assert warehouse.query_value("SELECT count(*) FROM base.location_point") == 0
        assert warehouse.location_point_ids() == set()
        assert run_loader(store, warehouse, source=source).records == 1
    finally:
        warehouse.close()


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({}, None),
        ({"motionactivities": None}, None),
        ({"motionactivities": []}, []),
        ({"motionactivities": ["walking", "future-state"]}, ["walking", "future-state"]),
    ],
)
def test_loader_preserves_motionactivities_and_missing_values(fields, expected):
    source, store, _, _ = location_store(**fields)
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate()
        assert run_loader(store, warehouse, source=source).records == 1
        assert run_loader(store, warehouse, source=source).skipped == 1
        assert warehouse.query_rows("SELECT motion_activities FROM base.location_point") == [
            (expected,)
        ]
    finally:
        warehouse.close()


def test_parser_correction_requires_explicit_reprocess():
    source, store, _, _ = location_store(motionactivities=["walking"])
    source.parser_version = "owntracks-location-v1"
    source.required_relations = ("base.location_point",)
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate()
        assert run_loader(store, warehouse, source=source).records == 1
        warehouse.connection.execute("UPDATE base.location_point SET motion_activities=NULL")
        source.parser_version = get_source("owntracks").parser_version

        assert run_reconciliation(store, warehouse, source=source).ok
        assert run_loader(store, warehouse, source=source, reprocess=True).records == 1
        assert warehouse.query_rows("SELECT motion_activities FROM base.location_point") == [
            (["walking"],)
        ]
        assert run_loader(store, warehouse, source=source).skipped == 1
    finally:
        warehouse.close()


def test_location_observed_at_retains_source_time_instead_of_storage_time():
    source, store, _, _ = location_store()
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.migrate()
        assert run_loader(store, warehouse, source=source).records == 1
        assert warehouse.query_rows(
            "SELECT observed_at AT TIME ZONE 'Asia/Tokyo' FROM base.location_point"
        ) == [(datetime(2026, 10, 9, 9),)]
    finally:
        warehouse.close()


def test_unlimited_raw_audit_repairs_missing_load_and_detects_deleted_raw(monkeypatch):
    source, store, _, _ = location_store()
    source.required_relations = ("base.location_point",)
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    try:
        result = run_reconciliation(store, warehouse, source=source)
        assert result.ok
        assert warehouse.query_value("SELECT count(*) FROM base.location_point") == 1
        store.objects.clear()
        result = run_reconciliation(store, warehouse, source=source)
        assert not result.ok
        assert result.details["missing_raw_object_count"] == 1
    finally:
        warehouse.close()


def test_location_decoder_rejects_key_timestamp_mismatch():
    source, store, key, _ = location_store()
    raw = store.list_raw(source.raw_prefixes[0])[0]
    message = json.loads(gzip.decompress(store.objects[key]))
    message["tst"] += 1
    with pytest.raises(ValueError, match="timestamp"):
        source.decode(raw, json.dumps(message).encode())
