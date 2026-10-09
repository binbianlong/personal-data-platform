import gzip
import json
from datetime import UTC, datetime

import pytest

from personal_data_platform.dbt_runner import run_dbt
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


def location_store():
    source = get_source("owntracks", "location")
    from personal_data_platform.sources.owntracks.raw import prepare_location

    key, payload, compressed = prepare_location(
        json.dumps(
            {"_type": "location", "tst": 1791504000, "lat": 35, "lon": 139, "acc": 7, "m": 1}
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
        assert run_loader(store, warehouse, source=source).records == 1
        assert run_loader(store, warehouse, source=source).skipped == 1
        assert warehouse.query_rows(
            "SELECT device_key,event_at,latitude,longitude,accuracy_m,monitoring_mode,object_key "
            "FROM base.owntracks_location"
        ) == [("a" * 64, datetime(2026, 10, 9, tzinfo=UTC), 35.0, 139.0, 7.0, 1, key)]
        assert warehouse.query_rows(
            "SELECT source_id,status,record_count FROM ops.ingestion_metadata"
        ) == [("owntracks", "succeeded", 1)]
        warehouse.migrate()
        assert warehouse.query_value("SELECT count(*) FROM base.owntracks_location") == 1
    finally:
        warehouse.close()


def test_failed_batch_rolls_back_location_and_can_be_retried(monkeypatch):
    source, store, _, _ = location_store()
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    from personal_data_platform.sources.owntracks.writer import OwnTracksBatch

    write = OwnTracksBatch.write

    def interrupted(self, connection, raw, **kwargs):
        write(self, connection, raw, **kwargs)
        raise RuntimeError("interrupted")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(OwnTracksBatch, "write", interrupted)
            assert run_loader(store, warehouse, source=source).failed == 1
        assert warehouse.query_value("SELECT count(*) FROM base.owntracks_location") == 0
        assert warehouse.query_value("SELECT status FROM ops.ingestion_metadata") == "failed"
        assert run_loader(store, warehouse, source=source).records == 1
    finally:
        warehouse.close()


def test_dbt_location_view_uses_tokyo_time(tmp_path, monkeypatch, dbt_project):
    source, store, _, _ = location_store()
    database = tmp_path / "owntracks.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    warehouse.migrate()
    run_loader(store, warehouse, source=source)
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project, selector="tag:owntracks")
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_rows(
            "SELECT event_at_jst,activity_date FROM marts.owntracks_location_time_series"
        ) == [(datetime(2026, 10, 9, 9), datetime(2026, 10, 9).date())]
    finally:
        warehouse.close()


def test_unlimited_raw_audit_repairs_missing_load_and_detects_deleted_raw(monkeypatch):
    source, store, _, _ = location_store()
    source.required_relations = ("base.owntracks_location",)
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    try:
        result = run_reconciliation(store, warehouse, source=source, heartbeat=lambda _: None)
        assert result.ok
        assert warehouse.query_value("SELECT count(*) FROM base.owntracks_location") == 1
        store.objects.clear()
        result = run_reconciliation(store, warehouse, source=source, heartbeat=lambda _: None)
        assert not result.ok
        assert result.details["premature_missing_object_count"] == 1
    finally:
        warehouse.close()


def test_location_decoder_rejects_key_timestamp_mismatch():
    source, store, key, _ = location_store()
    raw = store.list_raw(source.raw_prefixes[0])[0]
    message = json.loads(gzip.decompress(store.objects[key]))
    message["tst"] += 1
    with pytest.raises(ValueError, match="timestamp"):
        source.decode(raw, json.dumps(message).encode())
