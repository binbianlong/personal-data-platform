import gzip
import sqlite3
from datetime import UTC, datetime

import pytest

from personal_data_platform.cli import _collect_all
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

    def put_compressed_raw(self, key, compressed_bytes):
        self.raw.append((key, compressed_bytes))
        if self.fail_stream and f"/{self.fail_stream}/" in key:
            raise RuntimeError("synthetic upload failure")

    def put_scan_receipt(self, receipt):
        self.receipts.append(receipt)

    def put_device_manifest(self, manifest):
        self.manifests.append(manifest)


def _collectors(tmp_path):
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
        clock=lambda: NOW,
    )
    mac = ScreenTimeCollector(
        source=BiomeMacAppUsageSource(sync_db_path=sync_db, local_dir=local),
        state=state,
        uploader=uploader,
        pseudonym_key=SECRET,
        allowed_device_keys=frozenset({build_device_key(SECRET, "mac")}),
        clock=lambda: NOW,
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
    phone, _, uploader, _ = _collectors(tmp_path)
    uploader.fail_stream = "app-in-focus"

    with pytest.raises(ExceptionGroup, match="Screen Time collection failed") as error:
        _collect_all([phone], inactive_streams=("app-usage",), inactive_uploader=uploader)

    assert "app-in-focus" in str(error.value.exceptions[0])
    assert uploader.manifests[-1].stream == "app-usage"
    assert uploader.manifests[-1].device_keys == ()


def test_mac_only_collection_marks_iphone_explicitly_inactive(tmp_path) -> None:
    _, mac, uploader, _ = _collectors(tmp_path)

    assert (
        _collect_all([mac], inactive_streams=("app-in-focus",), inactive_uploader=uploader).uploaded
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
