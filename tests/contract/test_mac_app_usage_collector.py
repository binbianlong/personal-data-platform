"""Independent iPhone and Mac collection and loading contracts."""

import sqlite3
from datetime import UTC, datetime

import pytest

from personal_data_platform.biome.collector import ScreenTimeCollector
from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.biome.raw import build_device_key
from personal_data_platform.sources.iphone_screen_time.collector import BiomeScreenTimeSource
from personal_data_platform.sources.iphone_screen_time.runtime import load_pending as load_phone
from personal_data_platform.sources.mac_screen_time.collector import BiomeMacAppUsageSource
from personal_data_platform.sources.mac_screen_time.runtime import load_pending as load_mac
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import event, mac_usage_event, segb

NOW = datetime(2026, 10, 8, tzinfo=UTC)
SECRET = b"x" * 32


def _collectors(tmp_path):
    sync = tmp_path / "sync.db"
    with sqlite3.connect(sync) as c:
        c.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT, me INT)"
        )
        c.executemany(
            "INSERT INTO DevicePeer VALUES (?, ?, ?, ?, ?)",
            [("phone", "Phone", "P", 2, 0), ("mac", "Mac", "M", 3, 1)],
        )
    remote = tmp_path / "remote" / "phone"
    local = tmp_path / "local"
    remote.mkdir(parents=True)
    local.mkdir()
    (remote / "100").write_bytes(segb(event("phone.app"))[0])
    (local / "100").write_bytes(segb(mac_usage_event("mac.app", 1000, start=True))[0])
    states = [
        CollectorState(tmp_path / source / "collector.db", source_id=source)
        for source in ("iphone_screen_time", "mac_screen_time")
    ]
    collectors = [
        ScreenTimeCollector(
            source=source,
            state=state,
            pseudonym_key=SECRET,
            allowed_device_keys=frozenset({build_device_key(SECRET, device)}),
            clock=lambda: NOW,
        )
        for state, device, source in zip(
            states,
            ("phone", "mac"),
            (
                BiomeScreenTimeSource(sync_db_path=sync, remote_dir=remote.parent),
                BiomeMacAppUsageSource(sync_db_path=sync, local_dir=local),
            ),
            strict=True,
        )
    ]
    return collectors, states


def test_mac_and_phone_load_independently_and_publish_only_their_heartbeat(tmp_path):
    collectors, states = _collectors(tmp_path)
    for collector in collectors:
        assert collector.collect_once().deferred == 0
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    try:
        load_phone(states[0], wh, now=NOW)
        assert wh.query_rows("SELECT monitor_name FROM ops.heartbeat") == [
            ("iphone_screen_time_reconciliation",)
        ]
        assert len(states[1].pending()) == 1
        load_mac(states[1], wh, now=NOW)
        assert wh.query_rows("SELECT platform FROM base.iphone_screen_time_event") == [("ios",)]
        assert wh.query_rows("SELECT platform FROM base.mac_screen_time_event") == [("macos",)]
        assert all(state.pending() == [] for state in states)
    finally:
        wh.close()


def test_missing_mac_directory_does_not_prevent_phone_capture_and_load(tmp_path):
    collectors, states = _collectors(tmp_path)
    (tmp_path / "local" / "100").unlink()
    (tmp_path / "local").rmdir()
    with pytest.raises(RuntimeError, match="directory is missing"):
        collectors[1].collect_once()
    collectors[0].collect_once()
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    try:
        load_phone(states[0], wh, now=NOW)
        assert wh.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 1
        assert wh.query_value("SELECT count(*) FROM base.mac_screen_time_event") == 0
        assert states[1].last_successful_scan(stream="app-usage") is None
    finally:
        wh.close()


def test_phone_payload_failure_does_not_block_mac_capture_or_loading(tmp_path):
    collectors, states = _collectors(tmp_path)
    (tmp_path / "remote" / "phone" / "100").write_bytes(segb(event("invalid") + b"\xf3\x01")[0])
    for collector in collectors:
        collector.collect_once()
    phone_pending = states[0].pending()[0]
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    try:
        with pytest.raises(RuntimeError, match="Raw ingestion failed"):
            load_phone(states[0], wh, now=NOW)
        load_mac(states[1], wh, now=NOW)
        assert states[0].pending()[0] == phone_pending
        assert wh.query_value("SELECT count(*) FROM base.mac_screen_time_event") == 1
        assert wh.query_rows("SELECT monitor_name FROM ops.heartbeat") == [
            ("mac_screen_time_reconciliation",)
        ]
    finally:
        wh.close()


def test_mac_only_collection_does_not_mark_phone_inactive(tmp_path):
    collectors, states = _collectors(tmp_path)
    collectors[1].collect_once()
    assert states[0].last_successful_scan(stream="app-in-focus") is None
    assert states[1].last_successful_scan(stream="app-usage").device_keys


def test_mac_source_requires_exactly_one_local_device(tmp_path):
    sync = tmp_path / "sync.db"
    with sqlite3.connect(sync) as c:
        c.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT, me INT)"
        )
    with pytest.raises(RuntimeError, match="exactly one"):
        BiomeMacAppUsageSource(sync_db_path=sync, local_dir=tmp_path).list_devices()
