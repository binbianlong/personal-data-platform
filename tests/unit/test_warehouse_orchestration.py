"""Run ingestion using a warehouse with no database connection or SQL interface."""

import json
from datetime import UTC, datetime
from threading import Event

from personal_data_platform.loader.deadline import interrupt_after
from personal_data_platform.loader.job import run_loader
from personal_data_platform.sources.owntracks.raw import prepare_location
from personal_data_platform.sources.registry import get_source


def test_loader_retries_and_skips_using_a_connection_free_warehouse():
    source = get_source("owntracks", "location")
    key, _, compressed = prepare_location(
        json.dumps({"_type": "location", "tst": 1791504000, "lat": 35, "lon": 139}).encode(),
        device_key="a" * 64,
    )
    raw = source.parse_raw_key(key, storage_created_at=datetime.now(UTC), storage_generation=1)

    class Repository:
        def list_raw(self, prefix):
            return [raw] if key.startswith(prefix) else []

        def get_raw(self, key, *, generation):
            assert key == raw.key and generation == 1
            return compressed

    class MemoryWarehouse:
        connection_usable = True
        owner = None
        loaded = set()
        locations = {}
        interrupted = Event()

        def acquire_job_lock(self, job_name, owner_id, *, lease_seconds):
            assert job_name == "loader"
            if self.owner is not None:
                return False
            self.owner = owner_id
            return True

        def require_job_lock(self, owner_id, *, remaining_seconds):
            assert self.owner == owner_id and remaining_seconds > 0

        def release_job_lock(self, job_name, owner_id):
            assert self.owner == owner_id
            self.owner = None

        def succeeded_keys_for(self, observations, *, parser_version):
            return {value.key for value in observations} & self.loaded

        def load_object(self, value, *, byte_size, batch):
            assert value == raw and byte_size > 0
            self.locations[value.logical_key] = (batch.message["lat"], batch.message["lon"])
            self.loaded.add(value.key)
            return batch.record_count

        def interrupt(self):
            self.interrupted.set()

    warehouse = MemoryWarehouse()
    first = run_loader(Repository(), warehouse, source=source)
    repeated = run_loader(Repository(), warehouse, source=source)
    assert (first.succeeded, first.failed, first.records) == (1, 0, 1)
    assert (repeated.succeeded, repeated.skipped) == (0, 1)
    assert warehouse.locations == {raw.logical_key: (35, 139)}
    assert warehouse.owner is None
    timer = interrupt_after(warehouse, 0.01)
    timer.join(timeout=1)
    assert warehouse.interrupted.is_set()
