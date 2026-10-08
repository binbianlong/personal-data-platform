import gzip
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.collector import (
    BiomeScreenTimeSource,
    ScreenTimeCollector,
)
from personal_data_platform.sources.screen_time.raw import (
    CollectorDeviceManifest,
    CollectorScanReceipt,
    ScreenTimeRawIdentity,
    build_device_key,
    encode_segment_envelope,
)
from personal_data_platform.sources.screen_time.state import CollectorState
from personal_data_platform.sources.screen_time.storage import ScreenTimeLocalRepository
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import event, segb

NOW = datetime(2026, 10, 8, tzinfo=UTC)


@pytest.mark.parametrize("stream", ["app-in-focus", "app-usage"])
def test_local_control_objects_replace_latest_receipt_and_manifest(tmp_path, stream):
    state = CollectorState(tmp_path / "collector.db")
    repo = ScreenTimeLocalRepository(state=state, source=get_source("screen_time", stream))
    assert repo.get_device_manifest() is None
    original = CollectorScanReceipt("a" * 64, NOW, 1, stream=stream)
    latest = CollectorScanReceipt("a" * 64, NOW + timedelta(hours=24), 2, stream=stream)
    repo.put_scan_receipt(original)
    repo.put_device_manifest(CollectorDeviceManifest(("a" * 64,), NOW, stream=stream))
    repo.put_scan_receipt(latest)
    inactive = CollectorDeviceManifest((), NOW + timedelta(hours=24), stream=stream)
    repo.put_device_manifest(inactive)
    restarted = ScreenTimeLocalRepository(
        state=CollectorState(state.path), source=get_source("screen_time", stream)
    )
    assert restarted.list_scan_receipts() == [latest]
    assert restarted.get_device_manifest() == inactive


def test_loaded_raw_survives_restart_and_only_superseded_success_is_removed(tmp_path):
    state = CollectorState(tmp_path / "collector.db")
    args = dict(device_key="a" * 64, stream="app-in-focus", segment_key="b" * 64)
    first = state.prepare(**args, raw_bytes=b"first", observed_at=NOW)
    state.mark_uploaded(first.identity.object_key, NOW)
    second = state.prepare(**args, raw_bytes=b"second", observed_at=NOW + timedelta(seconds=1))
    third = state.prepare(**args, raw_bytes=b"third", observed_at=NOW + timedelta(seconds=2))
    repo = ScreenTimeLocalRepository(state=state, source=get_source())
    assert gzip.decompress(repo.get_raw(first.identity.object_key, generation=1)) == b"first"
    assert len(list(repo.list_raw("raw/screen_time/v1/"))) == 3
    state.mark_uploaded(second.identity.object_key, NOW)
    assert {r.key for r in repo.list_raw("raw/screen_time/v1/")} == {
        second.identity.object_key,
        third.identity.object_key,
    }
    state.mark_uploaded(third.identity.object_key, NOW)
    restarted = ScreenTimeLocalRepository(state=CollectorState(state.path), source=get_source())
    assert len(list(restarted.list_raw("raw/screen_time/v1/"))) == 1
    assert gzip.decompress(restarted.get_raw(third.identity.object_key, generation=1)) == b"third"
    with pytest.raises(ValueError, match="generation"):
        restarted.get_raw(third.identity.object_key, generation=2)


def test_latest_segment_is_saved_pending_without_successor(tmp_path):
    sync = tmp_path / "sync.db"
    with sqlite3.connect(sync) as c:
        c.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT)"
        )
        c.execute("INSERT INTO DevicePeer VALUES ('phone', 'Phone', 'P', 2)")
    remote = tmp_path / "remote" / "phone"
    remote.mkdir(parents=True)
    body, _ = segb(event("synthetic.app"))
    (remote / "100").write_bytes(body)
    state = CollectorState(tmp_path / "collector.db")
    repo = ScreenTimeLocalRepository(state=state, source=get_source())
    collector = ScreenTimeCollector(
        source=BiomeScreenTimeSource(sync_db_path=sync, remote_dir=remote.parent),
        state=state,
        uploader=repo,
        pseudonym_key=b"x" * 32,
        allowed_device_keys=frozenset({build_device_key(b"x" * 32, "phone")}),
        destination=str(state.path),
        clock=lambda: NOW,
    )
    assert collector.collect_once().deferred == 0
    pending = state.pending()
    assert len(pending) == 1
    assert gzip.decompress(pending[0].compressed_payload) == encode_segment_envelope(
        body, name="100", kind="events"
    )
    (remote / "100").write_bytes(segb(event("synthetic.other"), crc=0)[0])
    assert collector.collect_once().deferred == 1
    assert [p.identity.object_key for p in state.pending()] == [pending[0].identity.object_key]


@pytest.mark.parametrize("publish_success", [True, False])
def test_warehouse_commit_then_restart_acknowledges_without_duplicate_events(
    tmp_path, publish_success
):
    from personal_data_platform.sources.screen_time.cli import _load_pending

    state = CollectorState(tmp_path / "collector.db")
    body = encode_segment_envelope(segb(event("synthetic.app"))[0], name="100", kind="events")
    pending = state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        raw_bytes=body,
        observed_at=NOW,
        schema_version=2,
    )
    source = get_source()
    repo = ScreenTimeLocalRepository(state=state, source=source)
    repo.put_device_manifest(CollectorDeviceManifest(device_keys=("a" * 64,), completed_at=NOW))
    repo.put_scan_receipt(
        CollectorScanReceipt(device_key="a" * 64, completed_at=NOW, segment_count=1)
    )
    repo.put_device_manifest(
        CollectorDeviceManifest(device_keys=(), completed_at=NOW, stream="app-usage")
    )
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    from personal_data_platform.loader.job import run_loader_objects

    refs = tuple(repo.list_raw("raw/screen_time/v2/"))
    assert run_loader_objects(repo, wh, refs, source=source).ok
    assert state.pending()  # Process died before SQLite acknowledgement.
    _load_pending(state, wh, now=NOW, publish_success=publish_success)
    assert not state.pending()
    assert wh.query_value("SELECT count(*) FROM base.screen_time_event") == 1
    assert wh.query_value("SELECT count(*) FROM ops.heartbeat") == (2 if publish_success else 0)
    assert repo.get_raw(pending.identity.object_key, generation=1) == pending.compressed_payload
    wh.close()


def test_mac_audit_requires_its_own_receipt_even_when_iphone_is_fresh(tmp_path) -> None:
    from personal_data_platform.sources.screen_time.audit import audit_source

    now = datetime(2026, 9, 26, tzinfo=UTC)
    state = CollectorState(tmp_path / "collector.db")
    phone_repository = ScreenTimeLocalRepository(state=state, source=get_source())
    mac_repository = ScreenTimeLocalRepository(
        state=state,
        source=get_source("screen_time", "app-usage"),
    )
    phone_receipt = CollectorScanReceipt("a" * 64, now, 1)
    mac_receipt = CollectorScanReceipt("b" * 64, now, 1, stream="app-usage")
    phone_repository.put_scan_receipt(phone_receipt)
    phone_repository.put_device_manifest(CollectorDeviceManifest(("a" * 64,), now))
    mac_source = get_source("screen_time", "app-usage")
    assert mac_source.audit(mac_repository, [], now).ok

    mac_repository.put_device_manifest(
        CollectorDeviceManifest(("b" * 64,), now, stream="app-usage")
    )

    health = audit_source(mac_repository, [], now)
    assert not health.ok and health.details["missing_collector_receipt_count"] == 1

    mac_repository.put_scan_receipt(mac_receipt)
    assert audit_source(mac_repository, [], now).ok


def test_mac_audit_distinguishes_missing_manifest_from_explicit_deactivation(tmp_path) -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    state = CollectorState(tmp_path / "collector.db")
    source = get_source("screen_time", "app-usage")
    repository = ScreenTimeLocalRepository(state=state, source=source)
    receipt = CollectorScanReceipt("b" * 64, now, 1, stream="app-usage")
    repository.put_scan_receipt(receipt)
    assert not source.audit(repository, [], now).ok

    repository.put_device_manifest(
        CollectorDeviceManifest((), now - timedelta(days=2), stream="app-usage")
    )
    health = source.audit(repository, [], now)
    assert health.ok
    assert health.details["collector_inactive"] is True


def test_iphone_requires_manifest_until_explicitly_marked_inactive(tmp_path) -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    state = CollectorState(tmp_path / "collector.db")
    source = get_source("screen_time", "app-in-focus")
    repository = ScreenTimeLocalRepository(state=state, source=source)
    assert not source.audit(repository, [], now).ok

    repository.put_device_manifest(CollectorDeviceManifest((), now - timedelta(days=2)))
    health = source.audit(repository, [], now)
    assert health.ok
    assert health.details["collector_inactive"] is True


def test_mac_raw_without_manifest_is_not_treated_as_never_activated(tmp_path) -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    source = get_source("screen_time", "app-usage")
    repository = ScreenTimeLocalRepository(
        state=CollectorState(tmp_path / "collector.db"), source=source
    )
    raw_key = ScreenTimeRawIdentity(
        device_key="b" * 64,
        stream="app-usage",
        segment_key="c" * 64,
        observed_at=now,
        sha256="d" * 64,
        schema_version=2,
    ).object_key
    raw = source.parse_raw_key(raw_key, storage_created_at=now, storage_generation=1)
    assert not source.audit(repository, [raw], now).ok
