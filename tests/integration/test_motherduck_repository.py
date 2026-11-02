"""Warehouse source writes have atomic data and only two common ops tables."""

import time
from contextlib import closing
from dataclasses import replace

import pytest

from personal_data_platform.sources.owntracks import motherduck
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.integration.owntracks.test_owntracks_ingestion import location_store


def test_cloud_connection_is_scoped_to_the_requested_database(monkeypatch):
    import duckdb

    calls = []
    connection = object()

    def open_connection(database, *, config):
        calls.append((database, config))
        return connection

    monkeypatch.setattr(duckdb, "connect", open_connection)
    assert connect(WarehouseConfig("production", "synthetic-token")) is connection
    assert calls == [
        (
            "md:production",
            {"motherduck_token": "synthetic-token", "motherduck_attach_mode": "single"},
        )
    ]


def test_job_lease_rejects_a_second_owner(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "test.duckdb"))))
    try:
        warehouse.migrate()
        assert warehouse.acquire_job_lock("loader", "first", lease_seconds=60)
        assert not warehouse.acquire_job_lock("loader", "second", lease_seconds=60)

        warehouse.release_job_lock("loader", "not-the-owner")
        assert not warehouse.acquire_job_lock("loader", "second", lease_seconds=60)

        warehouse.release_job_lock("loader", "first")
        assert warehouse.acquire_job_lock("loader", "second", lease_seconds=60)
    finally:
        warehouse.close()


def test_migration_and_location_load_are_idempotent():
    source, store, _, _ = location_store()
    from personal_data_platform.loader.job import run_loader

    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        assert run_loader(store, warehouse, source=source).records == 1
        warehouse.migrate()
        assert run_loader(store, warehouse, source=source).skipped == 1
        assert len(warehouse.location_point_ids()) == 1
        assert {r for r in warehouse.relation_names() if r.startswith("ops.")} == {
            "ops.job_lock",
            "ops.schema_migration",
        }
        for obsolete in (
            "begin_job",
            "finish_job",
            "finish_collection",
            "mark_failed",
            "succeeded_keys_for",
            "prune_biome_receipts",
            "active_ingestion_states",
            "latest_completed_target_date",
        ):
            assert not hasattr(warehouse, obsolete)


@pytest.mark.parametrize("previously_loaded", [False, True])
def test_source_writer_failure_rolls_back_without_changing_existing_points(
    monkeypatch, previously_loaded
):
    source, store, _, payload = location_store()
    raw = store.list_raw(source.raw_prefixes[0])[0]
    batch = source.decode(raw, payload)
    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        assert warehouse.acquire_job_lock("loader", "owner", lease_seconds=120)
        kwargs = {"owner_id": "owner", "deadline": time.monotonic() + 10}
        if previously_loaded:
            warehouse.load_object(raw, batch=batch, **kwargs)
        before = warehouse.query_rows("SELECT * FROM base.location_point")
        writer = motherduck.write_batch

        def interrupted(*args, **kwargs):
            writer(*args, **kwargs)
            raise RuntimeError("source failed")

        monkeypatch.setattr(motherduck, "write_batch", interrupted)
        with pytest.raises(RuntimeError, match="source failed"):
            warehouse.load_object(
                raw, batch=replace(batch, message={**batch.message, "lat": 36}), **kwargs
            )
        assert warehouse.query_rows("SELECT * FROM base.location_point") == before
        assert warehouse.connection_usable


def test_location_id_lookup_can_filter_current_event_without_fetching_full_inventory():
    from personal_data_platform.loader.job import run_loader

    source, store, _, _ = location_store()
    raw = store.list_raw(source.raw_prefixes[0])[0]
    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        run_loader(store, warehouse, source=source)
        assert warehouse.location_point_ids([]) == set()
        assert warehouse.location_point_ids([raw.logical_key, "absent"]) == {raw.logical_key}
