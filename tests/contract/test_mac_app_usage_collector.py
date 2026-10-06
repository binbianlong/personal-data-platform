import gzip
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.screen_time.cli import _collect_all
from personal_data_platform.sources.screen_time.collector import (
    BiomeMacAppUsageSource,
    BiomeScreenTimeSource,
    ScreenTimeCollector,
)
from personal_data_platform.sources.screen_time.raw import build_device_key, decode_segment_envelope
from personal_data_platform.sources.screen_time.state import CollectorState

SECRET = b"x" * 32
NOW = datetime(2026, 9, 26, tzinfo=UTC)


class Uploader:
    def __init__(self):
        self.raw = []
        self.receipts = []
        self.manifests = []
        self.fail_stream = None
        self.fail_manifest_stream = None

    def put_compressed_raw(self, key, compressed_bytes):
        self.raw.append((key, compressed_bytes))
        if self.fail_stream and f"/{self.fail_stream}/" in key:
            raise RuntimeError("synthetic upload failure")

    def put_scan_receipt(self, receipt):
        self.receipts.append(receipt)

    def put_device_manifest(self, manifest):
        if self.fail_manifest_stream == manifest.stream:
            raise RuntimeError("synthetic manifest failure")
        self.manifests.append(manifest)


def _collectors(tmp_path, *, clock=lambda: NOW):
    sync_db = tmp_path / "sync.db"
    with sqlite3.connect(sync_db) as connection:
        connection.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT, me INT)"
        )
        connection.executemany(
            "INSERT INTO DevicePeer VALUES (?, ?, ?, ?, ?)",
            [("phone", "Phone", "P", 2, 0), ("mac", "Mac", "M", 3, 1)],
        )
    remote = tmp_path / "remote" / "phone"
    local = tmp_path / "local"
    remote.mkdir(parents=True)
    local.mkdir()
    for directory in (remote, local):
        (directory / "100").write_bytes(b"complete")
        (directory / "200").write_bytes(b"active")
    state = CollectorState(tmp_path / "state.db")
    uploader = Uploader()
    phone = ScreenTimeCollector(
        source=BiomeScreenTimeSource(sync_db_path=sync_db, remote_dir=remote.parent),
        state=state,
        uploader=uploader,
        pseudonym_key=SECRET,
        allowed_device_keys=frozenset({build_device_key(SECRET, "phone")}),
        destination="synthetic-bucket",
        clock=clock,
    )
    mac = ScreenTimeCollector(
        source=BiomeMacAppUsageSource(sync_db_path=sync_db, local_dir=local),
        state=state,
        uploader=uploader,
        pseudonym_key=SECRET,
        allowed_device_keys=frozenset({build_device_key(SECRET, "mac")}),
        destination="synthetic-bucket",
        clock=clock,
    )
    return phone, mac, uploader, state


def test_mac_and_iphone_upload_separate_streams_and_control_keys(tmp_path) -> None:
    phone, mac, uploader, state = _collectors(tmp_path)
    assert _collect_all([phone, mac]).uploaded == 2
    assert {key.split("/")[4] for key, _ in uploader.raw} == {"app-in-focus", "app-usage"}
    assert all(
        decode_segment_envelope(gzip.decompress(body))[0] == b"complete" for _, body in uploader.raw
    )
    assert {receipt.stream for receipt in uploader.receipts} == {"app-in-focus", "app-usage"}
    assert {manifest.key for manifest in uploader.manifests} == {
        "raw/screen_time/v1/_control/collector/active.json",
        "raw/screen_time/v1/_control/collector/app-usage/active.json",
    }
    assert state.pending() == []
    assert _collect_all([phone, mac]).skipped == 2


def test_failed_mac_upload_does_not_prevent_phone_and_retries_only_mac(tmp_path) -> None:
    phone, mac, uploader, state = _collectors(tmp_path)
    uploader.fail_stream = "app-usage"
    with pytest.raises(ExceptionGroup):
        _collect_all([mac, phone])
    assert uploader.receipts[-1].stream == "app-in-focus"
    pending = state.pending()
    assert len(pending) == 1 and pending[0].identity.stream == "app-usage"
    failed_key, failed_body = uploader.raw[0]

    uploader.fail_stream = None
    assert _collect_all([phone, mac]).retried == 1
    assert (failed_key, failed_body) in uploader.raw
    assert state.pending() == []


def test_disabled_mac_manifest_is_published_even_if_iphone_collection_fails(tmp_path) -> None:
    phone, _, uploader, state = _collectors(tmp_path)
    uploader.fail_stream = "app-in-focus"

    with pytest.raises(ExceptionGroup, match="Screen Time collection failed") as error:
        _collect_all(
            [phone],
            inactive_streams=("app-usage",),
            inactive_uploader=uploader,
            inactive_state=state,
            destination="synthetic-bucket",
        )

    assert "app-in-focus" in str(error.value.exceptions[0])
    assert uploader.manifests[-1].stream == "app-usage"
    assert uploader.manifests[-1].device_keys == ()


def test_mac_only_collection_marks_iphone_explicitly_inactive(tmp_path) -> None:
    _, mac, uploader, state = _collectors(tmp_path)

    assert (
        _collect_all(
            [mac],
            inactive_streams=("app-in-focus",),
            inactive_uploader=uploader,
            inactive_state=state,
            destination="synthetic-bucket",
        ).uploaded
        == 1
    )
    assert uploader.manifests[-1].stream == "app-in-focus"
    assert uploader.manifests[-1].device_keys == ()


def test_mac_source_requires_exactly_one_local_device(tmp_path) -> None:
    sync_db = tmp_path / "sync.db"
    with sqlite3.connect(sync_db) as connection:
        connection.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT, me INT)"
        )
    source = BiomeMacAppUsageSource(sync_db_path=sync_db, local_dir=tmp_path)
    with pytest.raises(RuntimeError, match="exactly one"):
        source.list_devices()


def test_both_streams_keep_control_clocks_across_sleep_and_preserve_newest_segment(tmp_path):
    now = [NOW]
    phone, mac, uploader, _ = _collectors(tmp_path, clock=lambda: now[0])
    assert _collect_all([phone, mac]).deferred == 2
    now[0] += timedelta(hours=23, minutes=59)
    for path in (tmp_path / "remote" / "phone" / "100", tmp_path / "local" / "100"):
        path.write_bytes(b"changed")
    stats = _collect_all([phone, mac])
    assert stats.uploaded == stats.deferred == 2
    assert len(uploader.receipts) == len(uploader.manifests) == 2
    now[0] = NOW + timedelta(hours=24)
    _collect_all([phone, mac])
    assert len(uploader.receipts) == len(uploader.manifests) == 4
    now[0] += timedelta(days=7)
    assert _collect_all([phone, mac]).deferred == 2
    assert len(uploader.receipts) == len(uploader.manifests) == 6
    assert all(receipt.completed_at == now[0] for receipt in uploader.receipts[-2:])


@pytest.mark.parametrize("stream", ["app-in-focus", "app-usage"])
def test_inactive_manifest_is_durable_and_reactivation_publishes_immediately(tmp_path, stream):
    now = [NOW]
    phone, mac, uploader, state = _collectors(tmp_path, clock=lambda: now[0])
    collector = phone if stream == "app-in-focus" else mac
    collector.collect_once()
    pending = state.prepare(
        device_key="f" * 64,
        stream=stream,
        segment_key="b" * 64,
        raw_bytes=b"pending",
        observed_at=NOW,
    )
    now[0] += timedelta(minutes=30)
    kwargs = dict(
        inactive_streams=(stream,),
        inactive_uploader=uploader,
        inactive_state=CollectorState(state.path),
        destination="synthetic-bucket",
        clock=lambda: now[0],
    )
    _collect_all([], **kwargs)
    assert uploader.manifests[-1].device_keys == ()
    assert state.pending()[0].compressed_payload == pending.compressed_payload
    _collect_all([], **kwargs)
    assert len(uploader.manifests) == 2
    now[0] += timedelta(minutes=30)
    assert collector.collect_once().retried == 1
    assert uploader.manifests[-1].device_keys
    assert uploader.receipts[-1].completed_at == now[0]
    assert len(uploader.manifests) == 3
    assert len(uploader.receipts) == 2


def test_inactive_publication_failure_retries_next_scan(tmp_path):
    _, _, uploader, state = _collectors(tmp_path)
    kwargs = dict(
        inactive_streams=("app-usage",),
        inactive_uploader=uploader,
        inactive_state=state,
        destination="synthetic-bucket",
        clock=lambda: NOW,
    )
    uploader.fail_manifest_stream = "app-usage"
    with pytest.raises(ExceptionGroup):
        _collect_all([], **kwargs)
    uploader.fail_manifest_stream = None
    _collect_all([], **kwargs)
    assert len(uploader.manifests) == 1
    _collect_all([], **kwargs)
    assert len(uploader.manifests) == 1
