import gzip
import sqlite3
from datetime import UTC, datetime

import pytest

from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.cli import _load_pending
from personal_data_platform.sources.screen_time.collector import (
    BiomeScreenTimeSource,
    CollectorSourceError,
    ScreenTimeCollector,
)
from personal_data_platform.sources.screen_time.raw import build_device_key, decode_segment_envelope
from personal_data_platform.sources.screen_time.state import CollectorState
from personal_data_platform.sources.screen_time.storage import ScreenTimeLocalRepository
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import event, segb

SECRET = b"x" * 32
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _collector(tmp_path):
    sync = tmp_path / "sync.db"
    with sqlite3.connect(sync) as c:
        c.execute(
            "CREATE TABLE DevicePeer (device_identifier TEXT, name TEXT, model TEXT, platform INT)"
        )
        c.executemany(
            "INSERT INTO DevicePeer VALUES (?, ?, ?, ?)",
            [("phone", "Phone", "P", 2), ("other", "Other", "P", 2)],
        )
    remote = tmp_path / "remote" / "phone"
    remote.mkdir(parents=True)
    state = CollectorState(tmp_path / "collector.db")
    repo = ScreenTimeLocalRepository(state=state, source=get_source())
    collector = ScreenTimeCollector(
        source=BiomeScreenTimeSource(sync_db_path=sync, remote_dir=remote.parent),
        state=state,
        uploader=repo,
        pseudonym_key=SECRET,
        allowed_device_keys=frozenset({build_device_key(SECRET, "phone")}),
        destination=str(state.path),
        clock=lambda: NOW,
    )
    return collector, state, repo, remote


def test_a_b_a_and_unchanged_snapshot_are_durable_and_idempotent(tmp_path):
    collector, state, repo, remote = _collector(tmp_path)
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    keys = []
    try:
        for bundle in ("a", "b", "a"):
            (remote / "100").write_bytes(segb(event(bundle))[0])
            collector.collect_once()
            keys.append(state.pending()[0].identity.object_key)
            _load_pending(state, wh, now=NOW)
            assert state.pending() == []
            assert len(list(repo.list_raw("raw/screen_time/v2/"))) == 1
        assert len(set(keys)) == 3
        assert collector.collect_once().skipped == 1
        assert state.pending() == []
        assert wh.query_value("SELECT count(*) FROM base.screen_time_event") == 2
    finally:
        wh.close()


def test_failed_transfer_keeps_every_changed_pending_snapshot(tmp_path):
    collector, state, repo, remote = _collector(tmp_path)
    (remote / "100").write_bytes(segb(event("a"))[0])
    collector.collect_once()
    original = state.pending()[0]
    collector.collect_once()
    assert len(state.pending()) == 1
    (remote / "100").write_bytes(segb(event("b"))[0])
    collector.collect_once()
    assert len(state.pending()) == 2
    assert state.pending()[0].compressed_payload == original.compressed_payload
    assert repo.get_raw(original.identity.object_key, generation=1) == original.compressed_payload


def test_crc_valid_unsupported_payload_remains_pending_for_parser_recovery(tmp_path):
    collector, state, repo, remote = _collector(tmp_path)
    body = segb(event("unknown-schema") + b"\xf3\x01")[0]
    (remote / "100").write_bytes(body)
    assert collector.collect_once().deferred == 0
    pending = state.pending()[0]
    (remote / "100").unlink()
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    try:
        with pytest.raises(CollectorSourceError, match="Raw ingestion failed"):
            _load_pending(state, wh, now=NOW)
        assert [p.identity.object_key for p in state.pending()] == [pending.identity.object_key]
        assert wh.query_value("SELECT count(*) FROM ops.heartbeat") == 0
        assert (
            decode_segment_envelope(
                gzip.decompress(repo.get_raw(pending.identity.object_key, generation=1))
            )[0]
            == body
        )
    finally:
        wh.close()


def test_competing_lease_preserves_pending_and_does_not_publish_heartbeat(tmp_path):
    collector, state, _, remote = _collector(tmp_path)
    (remote / "100").write_bytes(segb(event("a"))[0])
    collector.collect_once()
    wh = Warehouse(connect(WarehouseConfig(":memory:")))
    wh.migrate()
    try:
        wh.acquire_job_lock("loader", "competitor", lease_seconds=7500)
        with pytest.raises(RuntimeError, match="lease"):
            _load_pending(state, wh, now=NOW)
        assert len(state.pending()) == 1
        assert wh.query_value("SELECT count(*) FROM ops.heartbeat") == 0
        assert wh.query_value("SELECT owner_id FROM ops.job_lock") == "competitor"
    finally:
        wh.close()


def test_removed_source_file_keeps_its_local_raw(tmp_path):
    collector, state, repo, remote = _collector(tmp_path)
    body = segb(event("a"))[0]
    (remote / "100").write_bytes(body)
    collector.collect_once()
    raw = state.pending()[0]
    state.mark_uploaded(raw.identity.object_key, NOW)
    (remote / "100").unlink()
    collector.collect_once()
    assert (
        decode_segment_envelope(
            gzip.decompress(repo.get_raw(raw.identity.object_key, generation=1))
        )[0]
        == body
    )


@pytest.mark.parametrize(
    "body", [b"SEGB", segb(event("a"), crc=0)[0], segb(event("a"), state=2)[0]]
)
def test_invalid_snapshot_does_not_replace_valid_raw_or_advance_scan(tmp_path, body):
    collector, state, repo, remote = _collector(tmp_path)
    (remote / "100").write_bytes(segb(event("valid"))[0])
    collector.collect_once()
    previous = state.last_successful_scan()
    original = state.pending()[0]
    (remote / "100").write_bytes(body)
    assert collector.collect_once().deferred == 1
    assert state.last_successful_scan() == previous
    assert len(state.pending()) == 1
    assert repo.get_raw(original.identity.object_key, generation=1) == original.compressed_payload


def test_old_event_time_is_not_a_watermark(tmp_path):
    collector, state, _, remote = _collector(tmp_path)
    (remote / "200").write_bytes(segb(event("newer", timestamp=100))[0])
    collector.collect_once()
    (remote / "100").write_bytes(segb(event("late", timestamp=10))[0])
    collector.collect_once()
    assert len(state.pending()) == 2


def test_non_allowlisted_device_is_not_collected(tmp_path):
    collector, state, _, _ = _collector(tmp_path)
    other = tmp_path / "remote" / "other"
    other.mkdir()
    (other / "100").write_bytes(segb(event("other"))[0])
    collector.collect_once()
    assert state.pending() == []


def test_non_numeric_segment_is_a_scan_failure(tmp_path):
    collector, state, _, remote = _collector(tmp_path)
    (remote / "unknown").write_bytes(segb(event("a"))[0])
    with pytest.raises(CollectorSourceError, match="non-numeric"):
        collector.collect_once()
    assert state.last_successful_scan() is None
