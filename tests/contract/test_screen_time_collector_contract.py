import gzip
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.screen_time.collector import (
    BiomeScreenTimeSource,
    CollectionStats,
    CollectorSourceError,
    ScreenTimeCollector,
)
from personal_data_platform.sources.screen_time.raw import (
    CollectorDeviceManifest,
    CollectorScanReceipt,
    build_device_key,
    decode_segment_envelope,
)
from personal_data_platform.sources.screen_time.state import CollectorState

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


def _source_tree(tmp_path, *, successor: bool = True):
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
    segment = device_dir / "100"
    if successor:
        (device_dir / "200").write_bytes(b"still-active")
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
    assert [decode_segment_envelope(gzip.decompress(body))[0] for _, body in uploader.calls] == [
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


def test_waits_through_updates_and_restart_until_successor_exists(tmp_path, capsys) -> None:
    from personal_data_platform.cli import _print_collection_stats

    source, segment = _source_tree(tmp_path, successor=False)
    uploader = RecordingUploader()
    clock = AdvancingClock()
    collector = _collector(tmp_path, source, uploader, clock)
    for payload in (b"partial", b"appended"):
        segment.write_bytes(payload)
        assert collector.collect_once() == CollectionStats(devices=1, segments=1, deferred=1)
    assert uploader.calls == []
    state = CollectorState(tmp_path / "collector.db")
    assert state.pending() == []
    assert state.last_successful_scan() is not None
    assert len(uploader.receipts) == len(uploader.manifests) == 2
    assert uploader.receipts[-1].segment_count == 1

    clock.current += timedelta(days=30)
    restarted = _collector(tmp_path, source, uploader, clock)
    stats = restarted.collect_once()
    _print_collection_stats(stats)
    assert json.loads(capsys.readouterr().out)["deferred"] == 1
    assert uploader.calls == []

    (segment.parent / "200").write_bytes(b"next-active")
    assert restarted.collect_once().uploaded == 1
    assert [decode_segment_envelope(gzip.decompress(body))[0] for _, body in uploader.calls] == [
        b"appended"
    ]
    assert restarted.collect_once() == CollectionStats(devices=1, segments=2, skipped=1, deferred=1)


def test_initial_backfill_and_late_arrival_use_numeric_order(tmp_path) -> None:
    source, segment = _source_tree(tmp_path, successor=False)
    for name in ("9", "10", "100"):
        (segment.parent / name).write_bytes(name.encode())
    uploader = RecordingUploader()
    collector = _collector(tmp_path, source, uploader, AdvancingClock())
    assert collector.collect_once() == CollectionStats(
        devices=1, segments=3, uploaded=2, deferred=1
    )
    assert {decode_segment_envelope(gzip.decompress(body))[0] for _, body in uploader.calls} == {
        b"9",
        b"10",
    }
    (segment.parent / "8").write_bytes(b"late")
    assert collector.collect_once().uploaded == 1
    assert decode_segment_envelope(gzip.decompress(uploader.calls[-1][1]))[0] == b"late"


def test_devices_and_parent_directories_have_independent_successors(tmp_path) -> None:
    source, segment = _source_tree(tmp_path)
    segment.write_bytes(b"normal-complete")
    tombstone = segment.parent / "tombstone"
    tombstone.mkdir()
    (tombstone / "1").write_bytes(b"tombstone-complete")
    (tombstone / "2").write_bytes(b"tombstone-active")
    other_identifier = "other-iphone"
    with sqlite3.connect(source.sync_db_path) as connection:
        connection.execute(
            "INSERT INTO DevicePeer VALUES (?, 'Other', 'Synthetic', 2, 1)",
            (other_identifier,),
        )
    other_dir = source.remote_dir / other_identifier
    other_dir.mkdir()
    (other_dir / "1").write_bytes(b"other-active")
    uploader = RecordingUploader()
    collector = ScreenTimeCollector(
        source=source,
        state=CollectorState(tmp_path / "collector.db"),
        uploader=uploader,
        pseudonym_key=SECRET,
        allowed_device_keys=frozenset(
            build_device_key(SECRET, identifier)
            for identifier in (DEVICE_IDENTIFIER, other_identifier)
        ),
        clock=AdvancingClock(),
    )
    assert collector.collect_once() == CollectionStats(
        devices=2, segments=5, uploaded=2, deferred=3
    )
    assert {decode_segment_envelope(gzip.decompress(body))[0] for _, body in uploader.calls} == {
        b"normal-complete",
        b"tombstone-complete",
    }


def test_pending_is_retried_even_if_successor_disappears(tmp_path) -> None:
    source, segment = _source_tree(tmp_path)
    segment.write_bytes(b"durable")
    failed = RecordingUploader(fail_once=True)
    clock = AdvancingClock()
    with pytest.raises(RuntimeError, match="outage"):
        _collector(tmp_path, source, failed, clock).collect_once()
    (segment.parent / "200").unlink()
    segment.write_bytes(b"new-active-content")
    uploader = RecordingUploader()
    assert _collector(tmp_path, source, uploader, clock).collect_once() == CollectionStats(
        devices=1, segments=1, uploaded=1, retried=1, deferred=1
    )
    assert uploader.calls == failed.calls


@pytest.mark.parametrize("failure", ["unknown-name", "unstable", "permission", "missing"])
def test_incomplete_scan_does_not_advance_liveness(tmp_path, monkeypatch, failure) -> None:
    from personal_data_platform.sources.screen_time import collector as module

    source, segment = _source_tree(tmp_path)
    segment.write_bytes(b"complete")
    uploader = RecordingUploader()
    collector = _collector(tmp_path, source, uploader, AdvancingClock())
    collector.collect_once()
    previous = CollectorState(tmp_path / "collector.db").last_successful_scan()
    uploader.calls.clear()
    uploader.receipts.clear()
    uploader.manifests.clear()

    if failure == "unknown-name":
        (segment.parent / "unexpected").write_bytes(b"unknown")
    elif failure == "unstable":
        original_read = module.Path.read_bytes

        def changing_read(path):
            value = original_read(path)
            if path == segment:
                path.write_bytes(value + b"changed")
            return value

        monkeypatch.setattr(module.Path, "read_bytes", changing_read)
    else:
        nested = segment.parent / "tombstone"
        nested.mkdir()
        original_scandir = module.os.scandir

        def failed_scan(path):
            if module.Path(path) == nested:
                error = PermissionError if failure == "permission" else FileNotFoundError
                raise error("synthetic scan failure")
            return original_scandir(path)

        monkeypatch.setattr(module.os, "scandir", failed_scan)

    with pytest.raises(CollectorSourceError):
        collector.collect_once()
    assert uploader.calls == uploader.receipts == uploader.manifests == []
    assert CollectorState(tmp_path / "collector.db").last_successful_scan() == previous
