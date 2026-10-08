import sqlite3
from datetime import UTC, datetime

import pytest

from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.cli import _collect_all, _load_pending
from personal_data_platform.sources.screen_time.collector import (
    BiomeMacAppUsageSource,
    BiomeScreenTimeSource,
    ScreenTimeCollector,
)
from personal_data_platform.sources.screen_time.raw import build_device_key
from personal_data_platform.sources.screen_time.state import CollectorState
from personal_data_platform.sources.screen_time.storage import ScreenTimeLocalRepository
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
    state = CollectorState(tmp_path / "state.db")
    repo = ScreenTimeLocalRepository(state=state, source=get_source())
    collectors = [
        ScreenTimeCollector(
            source=source,
            state=state,
            uploader=repo,
            pseudonym_key=SECRET,
            allowed_device_keys=frozenset({build_device_key(SECRET, device)}),
            destination=str(state.path),
            clock=lambda: NOW,
        )
        for device, source in [
            ("phone", BiomeScreenTimeSource(sync_db_path=sync, remote_dir=remote.parent)),
            ("mac", BiomeMacAppUsageSource(sync_db_path=sync, local_dir=local)),
        ]
    ]
    return collectors, state, repo


def test_mac_and_iphone_share_state_and_keep_event_scopes_separate(tmp_path):
    collectors, state, _ = _collectors(tmp_path)
    assert _collect_all(collectors).deferred == 0
    assert {p.identity.stream for p in state.pending()} == {"app-in-focus", "app-usage"}
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    try:
        _load_pending(state, wh, now=NOW)
        assert wh.query_rows(
            "SELECT platform,count(*) FROM base.screen_time_event GROUP BY platform ORDER BY platform"
        ) == [("ios", 1), ("macos", 1)]
        assert state.pending() == []
        assert _collect_all(collectors).skipped == 2
    finally:
        wh.close()


def test_missing_mac_directory_does_not_prevent_phone_capture(tmp_path):
    collectors, state, _ = _collectors(tmp_path)
    (tmp_path / "local" / "100").unlink()
    (tmp_path / "local").rmdir()
    with pytest.raises(ExceptionGroup):
        _collect_all(list(reversed(collectors)))
    assert len(state.pending()) == 1
    assert state.pending()[0].identity.stream == "app-in-focus"


def test_mac_only_collection_records_explicit_iphone_inactivity(tmp_path):
    collectors, state, repo = _collectors(tmp_path)
    _collect_all(
        [collectors[1]],
        inactive_streams=("app-in-focus",),
        inactive_uploader=repo,
        inactive_state=state,
        destination=str(state.path),
        clock=lambda: NOW,
    )
    assert repo.get_device_manifest().device_keys == ()


def test_mac_source_requires_exactly_one_local_device(tmp_path):
    sync = tmp_path / "sync.db"
    with sqlite3.connect(sync) as c:
        c.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT, me INT)"
        )
    with pytest.raises(RuntimeError, match="exactly one"):
        BiomeMacAppUsageSource(sync_db_path=sync, local_dir=tmp_path).list_devices()
