import gzip
import sqlite3
from datetime import UTC, datetime

import pytest

from personal_data_platform.biome.collector import CollectorSourceError, ScreenTimeCollector
from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.loader.models import RawDecodeError
from personal_data_platform.sources.iphone_screen_time.adapter import IPhoneScreenTimeSource
from personal_data_platform.sources.iphone_screen_time.collector import (
    BiomeScreenTimeSource,
)
from personal_data_platform.sources.iphone_screen_time.raw import (
    build_device_key,
    decode_segment_envelope,
)
from personal_data_platform.sources.iphone_screen_time.runtime import load_pending
from personal_data_platform.sources.iphone_screen_time.storage import LocalRepository
from personal_data_platform.sources.registry import get_source
from tests.screen_time_helpers import ScreenTimeWarehouse, event, segb

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
    state = CollectorState(tmp_path / "collector.db", source_id="iphone_screen_time")
    repo = LocalRepository(state=state, source=get_source())
    collector = ScreenTimeCollector(
        source=BiomeScreenTimeSource(sync_db_path=sync, remote_dir=remote.parent),
        state=state,
        pseudonym_key=SECRET,
        allowed_device_keys=frozenset({build_device_key(SECRET, "phone")}),
        clock=lambda: NOW,
    )
    return collector, state, repo, remote


def test_a_b_a_and_unchanged_snapshot_are_durable_and_idempotent(tmp_path):
    collector, state, repo, remote = _collector(tmp_path)
    wh = ScreenTimeWarehouse()
    wh.migrate()
    keys = []
    try:
        for bundle in ("a", "b", "a"):
            (remote / "100").write_bytes(segb(event(bundle))[0])
            collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
            keys.append(state.pending()[0].identity.object_key)
            load_pending(state, wh)
            assert state.pending() == []
            assert len(list(repo.list_raw("raw/iphone_screen_time/v2/"))) == 0
        assert len(set(keys)) == 3
        assert (
            collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version).skipped
            == 1
        )
        assert state.pending() == []
        assert wh.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 2
    finally:
        wh.close()


def test_repeated_scans_keep_every_changed_pending_snapshot_until_load(tmp_path):
    collector, state, repo, remote = _collector(tmp_path)
    (remote / "100").write_bytes(segb(event("a"))[0])
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    original = state.pending()[0]
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    assert len(state.pending()) == 1
    (remote / "100").write_bytes(segb(event("b"))[0])
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    assert len(state.pending()) == 2
    assert state.pending()[0].compressed_payload == original.compressed_payload
    assert repo.get_raw(original.identity.object_key, generation=1) == original.compressed_payload


def test_collector_ignores_tombstone_files(tmp_path):
    collector, state, _, remote = _collector(tmp_path)
    (remote / "100").write_bytes(segb(event("app.current"))[0])
    tombstones = remote / "tombstone"
    tombstones.mkdir()
    (tombstones / "unneeded").write_bytes(b"not-an-event")

    result = collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)

    assert (result.segments, result.archived, result.deferred) == (1, 1, 0)
    assert len(state.pending()) == 1


def test_crc_valid_unsupported_payload_remains_pending_for_parser_recovery(tmp_path):
    collector, state, repo, remote = _collector(tmp_path)
    body = segb(event("unknown-schema") + b"\xf3\x01")[0]
    (remote / "100").write_bytes(body)
    assert (
        collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version).deferred == 0
    )
    pending = state.pending()[0]
    (remote / "100").unlink()
    wh = ScreenTimeWarehouse()
    wh.migrate()
    try:
        with pytest.raises(RawDecodeError):
            load_pending(state, wh)
        assert [p.identity.object_key for p in state.pending()] == [pending.identity.object_key]
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
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    wh = ScreenTimeWarehouse()
    wh.migrate()
    try:
        wh.acquire_job_lock("loader", "competitor", lease_seconds=7500)
        with pytest.raises(RuntimeError, match="lease"):
            load_pending(state, wh)
        assert len(state.pending()) == 1
        assert wh.locked
    finally:
        wh.close()


def test_removed_source_file_keeps_historical_events_and_refs(tmp_path):
    collector, state, repo, remote = _collector(tmp_path)
    (remote / "100").write_bytes(segb(event("a"))[0])
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    wh = ScreenTimeWarehouse()
    load_pending(state, wh)
    with state._connect() as db:
        refs = db.execute("SELECT * FROM event_reference").fetchall()
    (remote / "100").unlink()
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    load_pending(state, wh)
    assert wh.query_rows("SELECT bundle_id, is_valid FROM base.iphone_screen_time_event") == [
        ("a", True)
    ]
    with state._connect() as db:
        assert db.execute("SELECT * FROM event_reference").fetchall() == refs
    assert list(repo.list_raw("raw/iphone_screen_time/v2/")) == []
    wh.close()


@pytest.mark.parametrize(
    "body", [b"SEGB", segb(event("a"), crc=0)[0], segb(event("a"), state=2)[0]]
)
def test_invalid_snapshot_does_not_replace_valid_raw_or_advance_scan(tmp_path, body):
    collector, state, repo, remote = _collector(tmp_path)
    (remote / "100").write_bytes(segb(event("valid"))[0])
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    original = state.pending()[0]
    (remote / "100").write_bytes(body)
    assert (
        collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version).deferred == 1
    )
    assert len(state.pending()) == 1
    assert repo.get_raw(original.identity.object_key, generation=1) == original.compressed_payload


def test_old_event_time_is_not_a_watermark(tmp_path):
    collector, state, _, remote = _collector(tmp_path)
    (remote / "200").write_bytes(segb(event("newer", timestamp=100))[0])
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    (remote / "100").write_bytes(segb(event("late", timestamp=10))[0])
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    assert len(state.pending()) == 2


def test_non_allowlisted_device_is_not_collected(tmp_path):
    collector, state, _, _ = _collector(tmp_path)
    other = tmp_path / "remote" / "other"
    other.mkdir()
    (other / "100").write_bytes(segb(event("other"))[0])
    collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    assert state.pending() == []


def test_non_numeric_segment_is_a_scan_failure(tmp_path):
    collector, state, _, remote = _collector(tmp_path)
    (remote / "unknown").write_bytes(segb(event("a"))[0])
    with pytest.raises(CollectorSourceError, match="non-numeric"):
        collector.collect_once(parser_version=IPhoneScreenTimeSource.parser_version)
    assert state.pending() == []
