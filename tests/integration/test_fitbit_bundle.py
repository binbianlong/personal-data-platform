import gzip
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.loader.job import run_loader_objects
from personal_data_platform.sources.fitbit.adapter import FitbitSource
from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window
from personal_data_platform.sources.fitbit.raw import SnapshotBundle, encode_bundle, encode_snapshot
from personal_data_platform.storage.motherduck import Warehouse

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def snapshots():
    return (
        Snapshot(
            "self",
            Window("steps", NOW, NOW + timedelta(days=1)),
            NOW,
            (Record("steps", "steps", NOW, NOW, NOW + timedelta(minutes=1), 12),),
            source_payload=({"unknown": {"value": "retained"}},),
        ),
        Snapshot(
            "self",
            Window("heart-rate", NOW, NOW + timedelta(days=1)),
            NOW,
            (Record("heart-rate", "hr", NOW, NOW, value=65),),
        ),
    )


def test_one_bundle_replays_multiple_types_through_shared_loader_atomically():
    bundle = SnapshotBundle(snapshots())
    key, compressed = encode_bundle(bundle)
    source = FitbitSource()
    raw = source.parse_raw_key(key, storage_created_at=NOW, storage_generation=123)
    assert raw.schema_version == 2 and raw.logical_key == "batch"
    assert SnapshotBundle.from_bytes(gzip.decompress(compressed)) == bundle
    assert bundle.snapshots[0].source_payload[0]["unknown"] == {"value": "retained"}

    class Repository:
        calls = 0

        def list_raw(self, prefix):
            raise AssertionError("targeted loader must not list")

        def get_raw(self, key, *, generation):
            assert generation == 123
            self.calls += 1
            return compressed

    repository = Repository()
    warehouse = Warehouse(duckdb.connect())
    warehouse.migrate()
    try:
        result = run_loader_objects(repository, warehouse, (raw,), source=source)
        assert result.ok and result.records == 2
        assert warehouse.query_value("SELECT sum(value) FROM base.fitbit_steps") == 12
        assert warehouse.query_value("SELECT avg(value) FROM base.fitbit_heart_rate") == 65
        assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 1
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 2
        assert run_loader_objects(repository, warehouse, (raw,), source=source).skipped == 1
        assert repository.calls == 1
    finally:
        warehouse.close()


def test_bundle_rejects_mixed_subjects_and_overlapping_type_windows():
    first, second = snapshots()
    with pytest.raises(ValueError, match="subject"):
        SnapshotBundle((first, replace(second, subject_key="other")))
    with pytest.raises(ValueError, match="overlap"):
        SnapshotBundle((first, replace(first, fetched_at=NOW + timedelta(seconds=1))))
    with pytest.raises(ValueError, match="empty"):
        SnapshotBundle(())


def test_bundle_preserves_v1_and_rejects_mismatched_identity():
    source = FitbitSource()
    first = snapshots()[0]
    key, compressed = encode_snapshot(first)
    raw = source.parse_raw_key(key, storage_created_at=NOW, storage_generation=1)
    assert source.decode(raw, gzip.decompress(compressed)).snapshot == first
    key, compressed = encode_bundle(SnapshotBundle(snapshots()))
    raw = source.parse_raw_key(key, storage_created_at=NOW, storage_generation=1)
    with pytest.raises(ValueError, match="identity"):
        source.decode(replace(raw, subject_key="other"), gzip.decompress(compressed))


def test_bundle_rolls_back_all_types_if_second_write_fails(monkeypatch):
    from personal_data_platform.sources.fitbit.writer import FitbitBatch

    source = FitbitSource()
    key, compressed = encode_bundle(SnapshotBundle(snapshots()))
    raw = source.parse_raw_key(key, storage_created_at=NOW, storage_generation=1)
    warehouse = Warehouse(duckdb.connect())
    warehouse.migrate()
    original = FitbitBatch.write_snapshot

    def interrupted(self, *args, **kwargs):
        if self.snapshot.window.data_type == "heart-rate":
            raise RuntimeError("second type failed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FitbitBatch, "write_snapshot", interrupted)
    try:
        with pytest.raises(RuntimeError, match="second type failed"):
            warehouse.load_object(
                raw,
                byte_size=len(compressed),
                batch=source.decode(raw, gzip.decompress(compressed)),
            )
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") == 0
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 0
        assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 0
    finally:
        warehouse.close()
