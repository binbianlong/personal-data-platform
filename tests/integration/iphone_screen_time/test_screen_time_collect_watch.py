import sqlite3
from contextlib import closing

import pytest

from personal_data_platform.biome import runtime as biome_runtime
from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.sources.iphone_screen_time import runtime
from personal_data_platform.sources.iphone_screen_time.raw import build_device_key
from personal_data_platform.storage.factory import WarehouseConfig
from tests.screen_time_helpers import ScreenTimeWarehouse as Warehouse
from tests.screen_time_helpers import event, segb


@pytest.mark.parametrize("failure", [None, "connection", "migration", "lease"])
def test_watch_initializes_schema_once_and_recovers_pending(tmp_path, monkeypatch, failure):
    sync = tmp_path / "sync.db"
    with sqlite3.connect(sync) as connection:
        connection.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT)"
        )
        connection.execute("INSERT INTO DevicePeer VALUES ('phone', 'Phone', 'P', 2)")
    remote = tmp_path / "remote" / "phone"
    remote.mkdir(parents=True)
    segment = remote / "100"
    segment.write_bytes(segb(event("synthetic.first"))[0])
    state_path = tmp_path / "collector.db"
    state = CollectorState(state_path, source_id="iphone_screen_time")
    with state._connect() as connection:
        connection.execute("PRAGMA user_version = 1")
    for name, value in {
        "PDP_IPHONE_SCREEN_TIME_SYNC_DB_PATH": str(sync),
        "PDP_IPHONE_SCREEN_TIME_APP_IN_FOCUS_REMOTE_DIR": str(remote.parent),
        "PDP_IPHONE_SCREEN_TIME_STATE_DB_PATH": str(state_path),
        "PDP_IPHONE_SCREEN_TIME_PSEUDONYM_KEY_HEX": (b"x" * 32).hex(),
        "PDP_IPHONE_SCREEN_TIME_DEVICE_ALLOWLIST": build_device_key(b"x" * 32, "phone"),
        "PDP_SCREEN_TIME_MAC_DEVICE_KEY": "",
        "PDP_IPHONE_SCREEN_TIME_POLL_SECONDS": "1800",
    }.items():
        monkeypatch.setenv(name, value)
    warehouse_config = WarehouseConfig(str(tmp_path / "warehouse.duckdb"))
    monkeypatch.setattr(runtime, "warehouse_config", lambda: warehouse_config)
    with closing(Warehouse(warehouse_config.database)) as warehouse:
        warehouse.migrate()

    connections = []
    migrations = []
    leases = []
    real_migrate = Warehouse.migrate
    real_acquire = Warehouse.acquire_job_lock

    def open_connection(config):
        connections.append(config)
        if failure == "connection" and len(connections) == 1:
            raise ConnectionError("connection unavailable")
        return Warehouse(config.database)

    def migrate(warehouse, migration_path=None):
        migrations.append(warehouse)
        if failure == "migration" and len(migrations) == 1:
            raise ConnectionError("migration unavailable")
        real_migrate(warehouse)

    def acquire(warehouse, *args, **kwargs):
        leases.append(warehouse)
        if failure == "lease" and len(leases) == 1:
            return False
        return real_acquire(warehouse, *args, **kwargs)

    scans = []

    def next_scan(seconds):
        assert seconds == 1800
        scans.append(len(CollectorState(state_path, source_id="iphone_screen_time").pending()))
        if len(scans) == 1:
            segment.write_bytes(segb(event("synthetic.second"))[0])
        if len(scans) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(biome_runtime, "open_warehouse", open_connection)
    monkeypatch.setattr(Warehouse, "migrate", migrate)
    monkeypatch.setattr(Warehouse, "acquire_job_lock", acquire)
    monkeypatch.setattr(biome_runtime.time, "sleep", next_scan)

    assert runtime.collect(watch=True) == 0
    assert len(migrations) == (2 if failure == "migration" else 1)
    assert scans == ([1, 0, 0] if failure is not None else [0, 0, 0])
    warehouse = Warehouse(warehouse_config.database)
    try:
        assert warehouse.query_rows(
            "SELECT bundle_id FROM base.iphone_screen_time_event ORDER BY rowid"
        ) == [("synthetic.first",), ("synthetic.second",)]
    finally:
        warehouse.close()
