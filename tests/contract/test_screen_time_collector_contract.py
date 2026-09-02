import gzip
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.collectors.screen_time import (
    BiomeScreenTimeSource,
    CollectorSourceError,
    ScreenTimeCollector,
)
from personal_data_platform.collectors.state import CollectorState
from personal_data_platform.raw.screen_time import (
    CollectorDeviceManifest,
    CollectorScanReceipt,
    build_device_key,
)

SECRET = bytes.fromhex("42" * 32)
DEVICE_IDENTIFIER = "synthetic-iphone"


class RecordingUploader:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.receipts: list[CollectorScanReceipt] = []
        self.manifests: list[CollectorDeviceManifest] = []
        self.operations: list[str] = []
        self.fail_once = fail_once

    def put_compressed_raw(self, key: str, compressed_bytes: bytes) -> None:
        self.operations.append("raw")
        self.calls.append((key, compressed_bytes))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic GCS outage")

    def put_scan_receipt(self, receipt: CollectorScanReceipt) -> None:
        self.operations.append("receipt")
        self.receipts.append(receipt)

    def put_device_manifest(self, manifest: CollectorDeviceManifest) -> None:
        self.operations.append("manifest")
        self.manifests.append(manifest)


class AdvancingClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 27, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


def _source_tree(tmp_path):
    sync_db = tmp_path / "sync.db"
    with sqlite3.connect(sync_db) as connection:
        connection.execute(
            """
            CREATE TABLE DevicePeer (
                device_identifier STRING NOT NULL,
                name STRING,
                model STRING,
                platform INTEGER,
                protocol_version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO DevicePeer VALUES (?, 'Synthetic Phone', 'Synthetic1,1', 2, 1)
            """,
            (DEVICE_IDENTIFIER,),
        )
    remote = tmp_path / "remote"
    device_dir = remote / DEVICE_IDENTIFIER
    device_dir.mkdir(parents=True)
    segment = device_dir / "segment-001"
    source = BiomeScreenTimeSource(sync_db_path=sync_db, remote_dir=remote)
    return source, segment


def _collector(tmp_path, source, uploader, clock, *, allowlisted: bool = True):
    device_key = build_device_key(SECRET, DEVICE_IDENTIFIER)
    allowlist = frozenset({device_key}) if allowlisted else frozenset({"f" * 64})
    return ScreenTimeCollector(
        source=source,
        state=CollectorState(tmp_path / "collector.db"),
        uploader=uploader,
        pseudonym_key=SECRET,
        allowed_device_keys=allowlist,
        clock=clock,
    )


def test_collects_a_b_a_but_skips_consecutive_same_segment(tmp_path) -> None:
    source, segment = _source_tree(tmp_path)
    uploader = RecordingUploader()
    collector = _collector(tmp_path, source, uploader, AdvancingClock())

    segment.write_bytes(b"state-a")
    assert collector.collect_once().uploaded == 1
    assert collector.collect_once().skipped == 1
    segment.write_bytes(b"state-b")
    assert collector.collect_once().uploaded == 1
    segment.write_bytes(b"state-a")
    assert collector.collect_once().uploaded == 1

    keys = [key for key, _ in uploader.calls]
    assert len(keys) == 3
    assert len(set(keys)) == 3
    assert keys[0].rsplit("/", 1)[1] == keys[2].rsplit("/", 1)[1]
    assert all(DEVICE_IDENTIFIER not in key for key in keys)
    assert [gzip.decompress(body) for _, body in uploader.calls] == [
        b"state-a",
        b"state-b",
        b"state-a",
    ]
    assert len(uploader.receipts) == 4
    assert all(
        receipt.device_key == build_device_key(SECRET, DEVICE_IDENTIFIER)
        for receipt in uploader.receipts
    )
    assert len(uploader.manifests) == 4
    assert uploader.manifests[-1].device_keys == (build_device_key(SECRET, DEVICE_IDENTIFIER),)


def test_upload_failure_retries_the_same_key_and_bytes_after_restart(tmp_path) -> None:
    source, segment = _source_tree(tmp_path)
    segment.write_bytes(b"state-a")
    failing_uploader = RecordingUploader(fail_once=True)
    clock = AdvancingClock()
    collector = _collector(tmp_path, source, failing_uploader, clock)

    with pytest.raises(RuntimeError, match="synthetic GCS outage"):
        collector.collect_once()

    successful_uploader = RecordingUploader()
    restarted = _collector(tmp_path, source, successful_uploader, clock)
    stats = restarted.collect_once()

    assert stats.retried == 1
    assert failing_uploader.calls[0] == successful_uploader.calls[0]
    assert len(successful_uploader.receipts) == 1
    assert len(successful_uploader.manifests) == 1


def test_decommissioned_device_pending_is_retried_before_manifest_update(tmp_path) -> None:
    source, segment = _source_tree(tmp_path)
    segment.write_bytes(b"state-a")
    failing_uploader = RecordingUploader(fail_once=True)
    clock = AdvancingClock()

    with pytest.raises(RuntimeError, match="synthetic GCS outage"):
        _collector(tmp_path, source, failing_uploader, clock).collect_once()

    active_identifier = "synthetic-active-iphone"
    with sqlite3.connect(source.sync_db_path) as connection:
        connection.execute(
            "INSERT INTO DevicePeer VALUES (?, 'Active Phone', 'Synthetic2,1', 2, 1)",
            (active_identifier,),
        )
    (source.remote_dir / active_identifier).mkdir()

    active_device_key = build_device_key(SECRET, active_identifier)
    state = CollectorState(tmp_path / "collector.db")
    successful_uploader = RecordingUploader()
    restarted = ScreenTimeCollector(
        source=source,
        state=state,
        uploader=successful_uploader,
        pseudonym_key=SECRET,
        allowed_device_keys=frozenset({active_device_key}),
        clock=clock,
    )

    stats = restarted.collect_once()

    assert stats.retried == 1
    assert stats.uploaded == 1
    assert failing_uploader.calls[0] == successful_uploader.calls[0]
    assert state.pending() == []
    assert successful_uploader.operations == ["raw", "receipt", "manifest"]
    assert [receipt.device_key for receipt in successful_uploader.receipts] == [active_device_key]
    assert successful_uploader.manifests[-1].device_keys == (active_device_key,)


def test_non_allowlisted_device_is_not_collected(tmp_path) -> None:
    source, segment = _source_tree(tmp_path)
    segment.write_bytes(b"state-a")
    uploader = RecordingUploader()
    collector = _collector(
        tmp_path,
        source,
        uploader,
        AdvancingClock(),
        allowlisted=False,
    )

    with pytest.raises(CollectorSourceError, match="no allowlisted"):
        collector.collect_once()

    assert uploader.calls == []


def test_manifest_keeps_the_full_allowlist_when_one_device_is_not_discovered(tmp_path) -> None:
    source, segment = _source_tree(tmp_path)
    segment.write_bytes(b"state-a")
    discovered_key = build_device_key(SECRET, DEVICE_IDENTIFIER)
    undiscovered_key = "f" * 64
    uploader = RecordingUploader()
    collector = ScreenTimeCollector(
        source=source,
        state=CollectorState(tmp_path / "collector.db"),
        uploader=uploader,
        pseudonym_key=SECRET,
        allowed_device_keys=frozenset({discovered_key, undiscovered_key}),
        clock=AdvancingClock(),
    )

    assert collector.collect_once().devices == 1
    assert [receipt.device_key for receipt in uploader.receipts] == [discovered_key]
    assert uploader.manifests[-1].device_keys == tuple(sorted((discovered_key, undiscovered_key)))


def test_missing_directory_for_one_allowlisted_device_fails_the_complete_scan(tmp_path) -> None:
    source, segment = _source_tree(tmp_path)
    segment.write_bytes(b"state-a")
    missing_identifier = "synthetic-iphone-without-stream"
    with sqlite3.connect(source.sync_db_path) as connection:
        connection.execute(
            "INSERT INTO DevicePeer VALUES (?, 'Missing Stream', 'Synthetic2,1', 2, 1)",
            (missing_identifier,),
        )
    uploader = RecordingUploader()
    collector = ScreenTimeCollector(
        source=source,
        state=CollectorState(tmp_path / "collector.db"),
        uploader=uploader,
        pseudonym_key=SECRET,
        allowed_device_keys=frozenset(
            {
                build_device_key(SECRET, DEVICE_IDENTIFIER),
                build_device_key(SECRET, missing_identifier),
            }
        ),
        clock=AdvancingClock(),
    )

    with pytest.raises(CollectorSourceError, match="1 allowlisted device"):
        collector.collect_once()

    assert uploader.calls == []
    assert uploader.receipts == []
