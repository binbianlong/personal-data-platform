"""Generation-pinned loading retries invalid Raw and preserves shared leases."""

import time
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest

from personal_data_platform.loader.job import (
    JobAlreadyRunning,
    run_loader,
    run_loader_all,
    run_loader_objects,
)
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.integration.owntracks.test_location_point import Store


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", "other"),
        ("stream", "other"),
        ("schema_version", 3),
        ("subject_key", "b" * 64),
        ("logical_key", "wrong"),
    ],
)
def test_loader_rejects_inconsistent_metadata_before_reading(field, value):
    store = Store()
    raw = store.add()
    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        with pytest.raises(ValueError):
            run_loader_objects(
                store, warehouse, [replace(raw, **{field: value})], source=get_source("owntracks")
            )
        assert not store.reads
        assert not warehouse.location_point_ids()


def test_loader_continues_after_poison_object_and_retries_it():
    store = Store()
    first, second = store.add(), store.add(lat=36)
    original = store.objects[first.key]
    store.objects[first.key] = first, b"corrupt"
    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        outcome = run_loader(store, warehouse, source=get_source("owntracks"))
        assert (outcome.failed, outcome.succeeded, outcome.records) == (1, 1, 1)
        store.objects[first.key] = original
        outcome = run_loader(store, warehouse, source=get_source("owntracks"))
        assert (outcome.failed, outcome.succeeded, outcome.skipped) == (0, 1, 1)
        assert warehouse.location_point_ids() == {first.logical_key, second.logical_key}


def test_scheduled_loader_defers_without_reading_when_shared_lease_is_busy():
    store = Store()
    store.add()
    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        warehouse.acquire_job_lock("loader", "other", lease_seconds=7500)
        with pytest.raises(JobAlreadyRunning):
            run_loader(store, warehouse, source=get_source("owntracks"))
        assert not store.reads
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "other"


def test_all_streams_loader_keeps_a_single_owner_and_stops_on_unknown_commit(monkeypatch):
    store = Store()
    store.add()
    source = get_source("owntracks")
    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        calls = []
        connection = warehouse.connection

        class LostCommit:
            def execute(self, sql, *args):
                result = connection.execute(sql, *args)
                if (
                    sql == "COMMIT"
                    and connection.execute("SELECT count(*) FROM base.location_point").fetchone()[0]
                ):
                    raise OSError("response lost")
                return result

            def __getattr__(self, name):
                return getattr(connection, name)

        warehouse.connection = LostCommit()

        def repository_factory(source):
            calls.append(source.source_id)
            return store

        outcome = run_loader_all(
            [source, source], warehouse=warehouse, repository_factory=repository_factory
        )
        assert not outcome.ok and not warehouse.connection_usable
        assert calls == ["owntracks"]
        assert len(warehouse.location_point_ids()) == 1
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") is not None


def test_inherited_loader_lease_stops_after_owner_loss(monkeypatch):
    from types import SimpleNamespace

    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.sources.registry import get_source
    from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    assert warehouse.acquire_job_lock("loader", "other", lease_seconds=7500)
    try:
        with pytest.raises(RuntimeError, match="lease"):
            run_loader_objects(
                SimpleNamespace(),
                warehouse,
                (),
                source=get_source("owntracks"),
                _lease_owner="daily",
            )
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "other"
    finally:
        warehouse.close()


def test_warehouse_deadline_interrupts_a_blocking_query():

    import duckdb

    from personal_data_platform.loader.deadline import interrupt_after
    from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    started = time.monotonic()
    timer = interrupt_after(warehouse, 0.05)
    try:
        with pytest.raises(duckdb.InterruptException):
            warehouse.query_value("SELECT sum(i) FROM range(1000000000000) t(i)")
        assert time.monotonic() - started < 5
    finally:
        timer.cancel()
        warehouse.close()


def test_loader_deadline_interrupts_database_work(monkeypatch):

    from personal_data_platform.loader.deadline import interrupt_after
    from personal_data_platform.loader.job import run_loader_objects
    from personal_data_platform.sources.registry import get_source

    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    warehouse.migrate()
    monkeypatch.setattr(
        warehouse,
        "location_point_ids",
        lambda *args, **kwargs: warehouse.query_value(
            "SELECT sum(i) FROM range(1000000000000) t(i)"
        ),
    )
    started = time.monotonic()
    fallback = interrupt_after(warehouse, 3)
    try:
        with pytest.raises(Exception):
            run_loader_objects(
                SimpleNamespace(),
                warehouse,
                (),
                source=get_source("owntracks"),
                _deadline=started + 1.2,
            )
        assert time.monotonic() - started < 2
        assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    finally:
        fallback.cancel()
        warehouse.close()
