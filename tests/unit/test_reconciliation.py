"""OwnTracks reconciliation compares provider point IDs without receipts."""

from datetime import UTC, datetime

import pytest

from personal_data_platform.loader.models import LoadSummary
from personal_data_platform.reconciliation.job import run_reconciliation
from personal_data_platform.sources.owntracks.adapter import OwnTracksSource
from personal_data_platform.sources.owntracks.raw import prepare_location
from personal_data_platform.sources.registry import get_source

NOW = datetime(2026, 10, 9, tzinfo=UTC)


def raw(latitude=35):
    import json

    key, _, _ = prepare_location(
        json.dumps(
            {"_type": "location", "tst": int(NOW.timestamp()), "lat": latitude, "lon": 139}
        ).encode(),
        device_key="a" * 64,
    )
    return OwnTracksSource().parse_raw_key(key, storage_created_at=NOW, storage_generation=1)


class Repository:
    def __init__(self, refs=()):
        self.refs = list(refs)

    def list_raw(self, prefix):
        return [r for r in self.refs if r.key.startswith(prefix)]


class Warehouse:
    def __init__(self, loaded=(), *, relations=("base.location_point",)):
        self.loaded = set(loaded)
        self.relations = set(relations)

    def location_point_ids(self, ids=None):
        return self.loaded.copy() if ids is None else self.loaded & set(ids)

    def relation_names(self):
        return self.relations.copy()

    def check_relation(self, relation):
        assert relation in self.relations


def test_success_returns_id_parity_without_new_operational_state():
    first, second = raw(), raw(36)
    warehouse = Warehouse([first.logical_key, second.logical_key])
    result = run_reconciliation(
        Repository([first, second]), warehouse, repair_missing=False, now=NOW
    )
    assert result.ok
    assert result.loaded_object_count == result.raw_object_count == 2
    assert warehouse.loaded == {first.logical_key, second.logical_key}


def test_missing_and_orphaned_ids_block_success_without_fake_failure_registry():
    first, second = raw(), raw(36)
    result = run_reconciliation(
        Repository([first]), Warehouse([second.logical_key]), repair_missing=False
    )
    assert not result.ok
    assert result.missing_object_count == result.orphaned_loaded_object_count == 1
    assert result.failed_object_count == 0
    assert result.details["missing_raw_object_count"] == 1
    assert "unrecoverable_uningested_object_count" not in result.details


def test_repair_rechecks_new_uploads_before_classifying_orphans(monkeypatch):
    first, second, third = raw(), raw(36), raw(37)
    repository = Repository([first])
    warehouse = Warehouse()

    def repair(repository, warehouse, refs, *, source, _lease_owner):
        assert list(refs) == [first]
        repository.refs.extend([second, third])
        warehouse.loaded.update([first.logical_key, second.logical_key])
        return LoadSummary(discovered=2, skipped=0, succeeded=2, failed=0, records=2)

    monkeypatch.setattr("personal_data_platform.reconciliation.job.run_loader_objects", repair)
    result = run_reconciliation(repository, warehouse, now=NOW)
    assert result.ok
    assert result.raw_object_count == result.loaded_object_count == 2
    assert result.details["missing_before_repair"] == 1


def test_supplied_inventory_is_not_refreshed_and_generation_refs_reach_repair(monkeypatch):
    first = raw()
    warehouse = Warehouse()

    def repair(repository, warehouse, refs, *, source, _lease_owner):
        assert refs == [first]
        assert refs[0].storage_generation == 1
        warehouse.loaded.add(first.logical_key)
        return LoadSummary(1, 0, 1, 0, 1)

    monkeypatch.setattr("personal_data_platform.reconciliation.job.run_loader_objects", repair)

    class NoList:
        def list_raw(self, prefix):
            raise AssertionError("unexpected relist")

    assert run_reconciliation(NoList(), warehouse, raw_objects=(first,)).ok


def test_failed_repair_is_reported_only_from_current_attempt(monkeypatch):
    first = raw()
    monkeypatch.setattr(
        "personal_data_platform.reconciliation.job.run_loader_objects",
        lambda *a, **k: LoadSummary(1, 0, 0, 1, 0),
    )
    result = run_reconciliation(Repository([first]), Warehouse())
    assert not result.ok and result.failed_object_count == 1
    assert result.missing_object_count == 1


def test_missing_relation_blocks_success():
    result = run_reconciliation(Repository(), Warehouse(relations=()), repair_missing=False)
    assert not result.ok
    assert result.missing_relations == ("base.location_point",)


def test_invalid_time_or_partial_inventory_fails_before_warehouse_access():
    source = OwnTracksSource()
    with pytest.raises(ValueError, match="timezone-aware"):
        run_reconciliation(Repository(), object(), now=datetime(2026, 10, 9))
    with pytest.raises(ValueError, match="every canonical prefix"):
        run_reconciliation(
            Repository(), object(), source=source, prefix=source.raw_prefixes[0] + "a" * 64 + "/"
        )


@pytest.mark.parametrize("source_id", ["iphone_screen_time", "mac_screen_time"])
def test_screen_time_history_does_not_use_pending_files_for_raw_parity(source_id):
    with pytest.raises(ValueError, match="canonical in base"):
        run_reconciliation(Repository(), object(), source=get_source(source_id))
