import gzip
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from personal_data_platform.biome.collector_state import CollectorState

NOW = datetime(2026, 8, 27, 1, 2, 3, 123456, tzinfo=UTC)


def prepare(state, body=b"a", *, segment="b" * 64, parser="v1"):
    return state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key=segment,
        raw_bytes=body,
        observed_at=NOW,
        parser_version=parser,
    )


def ack(state, pending, keys=("event",)):
    state.pin_parser(pending.identity.object_key, "v1")
    state.mark_loaded(pending.identity.object_key, event_keys=keys)


def test_exact_schema_and_external_private_payload(tmp_path):
    state = CollectorState(tmp_path / "private" / "collector.db", source_id="iphone_screen_time")
    pending = prepare(state)
    with state._connect() as db:
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "file_state",
            "event_reference",
        }
        row = db.execute("SELECT observed_at, payload_path FROM file_state").fetchone()
    assert row[0] == NOW.isoformat()
    path = Path(row[1])
    assert path.read_bytes() == pending.compressed_payload
    assert gzip.decompress(path.read_bytes()) == b"a"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    ack(state, pending)
    assert not path.exists()
    with state._connect() as db:
        assert db.execute("SELECT status, payload_path FROM file_state").fetchone() == (
            "sent",
            None,
        )
        assert db.execute("SELECT * FROM event_reference").fetchall() == [("b" * 64, "event")]


def test_restart_consecutive_duplicate_and_a_b_a(tmp_path):
    path = tmp_path / "state.db"
    state = CollectorState(path, source_id="iphone_screen_time")
    first = prepare(state)
    state = CollectorState(path, source_id="iphone_screen_time")
    assert prepare(state).identity == first.identity
    ack(state, first)
    assert prepare(state) is None
    second = prepare(state, b"b")
    third = prepare(state, b"a")
    assert third.identity != first.identity
    ack(state, second)
    assert [p.identity for p in state.pending()] == [third.identity]
    ack(state, third)
    with state._connect() as db:
        assert db.execute("SELECT count(*) FROM file_state").fetchone() == (1,)


def test_parser_correction_requires_same_content_and_unshared_reference(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    first = prepare(state)
    ack(state, first, ("shared", "removed"))
    other = prepare(state, segment="c" * 64)
    ack(state, other, ("shared",))
    correction = prepare(state, parser="v2")
    state.pin_parser(correction.identity.object_key, "v2")
    assert state.invalidated_event_keys(correction.identity.object_key, ()) == ["removed"]
    state.mark_loaded(correction.identity.object_key, event_keys=())
    changed = prepare(state, b"different", segment="c" * 64, parser="v2")
    state.pin_parser(changed.identity.object_key, "v2")
    assert state.invalidated_event_keys(changed.identity.object_key, ()) == []


def test_missing_or_corrupt_pending_stops_and_keeps_state(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    p = prepare(state)
    with state._connect() as db:
        path = Path(db.execute("SELECT payload_path FROM file_state").fetchone()[0])
    path.write_bytes(gzip.compress(b"corrupt"))
    with pytest.raises(RuntimeError, match="hash"):
        state.pending()
    path.unlink()
    with pytest.raises(RuntimeError, match="payload"):
        state.pending()
    with state._connect() as db:
        assert db.execute("SELECT snapshot_key, status FROM file_state").fetchone() == (
            p.identity.object_key,
            "pending",
        )


def test_crash_after_ack_reclaims_only_sent_payload(tmp_path, monkeypatch):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    first = prepare(state)
    second = prepare(state, b"b")
    original = state.cleanup_sent_payloads

    def fail_after_ack():
        with state._connect() as db:
            sent = db.execute("SELECT count(*) FROM file_state WHERE status='sent'").fetchone()[0]
        if sent:
            raise OSError("crash")
        original()

    monkeypatch.setattr(state, "cleanup_sent_payloads", fail_after_ack)
    with pytest.raises(OSError, match="crash"):
        ack(state, first)
    monkeypatch.setattr(state, "cleanup_sent_payloads", original)
    state = CollectorState(state.path, source_id="iphone_screen_time")
    state.cleanup_sent_payloads()
    assert [p.identity for p in state.pending()] == [second.identity]
    with state._connect() as db:
        assert db.execute("SELECT payload_path FROM file_state WHERE status='sent'").fetchone() == (
            None,
        )


def test_pin_cannot_change_on_uncertain_retry_and_ack_is_ordered(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    first = prepare(state)
    second = prepare(state, b"b")
    state.pin_parser(first.identity.object_key, "v1")
    with pytest.raises(RuntimeError, match="parser"):
        state.pin_parser(first.identity.object_key, "v2")
    with pytest.raises(RuntimeError, match="oldest"):
        ack(state, second)
    assert len(state.pending()) == 2


def test_legacy_database_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE segment_observation (payload BLOB)")
        db.execute("INSERT INTO segment_observation VALUES (X'01')")
    with pytest.raises(RuntimeError, match="migration"):
        CollectorState(path, source_id="iphone_screen_time")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM segment_observation").fetchall() == [(b"\x01",)]


def test_source_lock_contention_and_release(tmp_path):
    first = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    second = CollectorState(first.path, source_id="iphone_screen_time")
    with first.lock():
        with pytest.raises(RuntimeError, match="collector"):
            with second.lock():
                pytest.fail("must not enter")
    with second.lock():
        pass


def test_invalid_deflate_pending_fails_closed_and_retains_bytes(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    pending = prepare(state)
    with state._connect() as db:
        path = Path(db.execute("SELECT payload_path FROM file_state").fetchone()[0])
    broken = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff\x07" + b"\x00" * 8
    path.write_bytes(broken)
    with pytest.raises(RuntimeError, match="corrupt"):
        state.pending()
    assert path.read_bytes() == broken
    with state._connect() as db:
        assert db.execute("SELECT snapshot_key, status FROM file_state").fetchone() == (
            pending.identity.object_key,
            "pending",
        )


def test_payload_durable_before_pending_commit_failure_is_retained(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    with state._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_pending BEFORE INSERT ON file_state BEGIN SELECT RAISE(ABORT, 'pending commit failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="pending commit failed"):
        prepare(state)
    retained = list(state.payload_directory.iterdir())
    assert len(retained) == 1
    assert gzip.decompress(retained[0].read_bytes()) == b"a"
    state = CollectorState(state.path, source_id="iphone_screen_time")
    state.cleanup_sent_payloads()
    assert state.pending() == []
    assert retained[0].exists()
    with state._connect() as db:
        assert db.execute("SELECT count(*) FROM file_state").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM event_reference").fetchone() == (0,)


def test_cleanup_does_not_remove_unrecognized_spool_bytes(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    unknown = state.payload_directory / "unknown.segb.gz"
    unknown.write_bytes(b"sole retained copy")
    pending = prepare(state)
    ack(state, pending)
    assert unknown.read_bytes() == b"sole retained copy"
    assert list(state.payload_directory.iterdir()) == [unknown]


def test_ack_reference_failure_rolls_back_and_retains_pending_payload(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    pending = prepare(state)
    with state._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_reference BEFORE INSERT ON event_reference BEGIN SELECT RAISE(ABORT, 'reference commit failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="reference commit failed"):
        ack(state, pending)
    assert state.pending()[0].compressed_payload == pending.compressed_payload
    with state._connect() as db:
        assert db.execute("SELECT status FROM file_state").fetchone() == ("pending",)
        assert db.execute("SELECT count(*) FROM event_reference").fetchone() == (0,)


def test_cleanup_retries_after_unlink_before_path_clear(tmp_path):
    state = CollectorState(tmp_path / "state.db", source_id="iphone_screen_time")
    pending = prepare(state)
    with state._connect() as db:
        path = Path(db.execute("SELECT payload_path FROM file_state").fetchone()[0])
        db.execute(
            "CREATE TRIGGER fail_clear BEFORE UPDATE OF payload_path ON file_state WHEN NEW.payload_path IS NULL BEGIN SELECT RAISE(ABORT, 'path clear failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="path clear failed"):
        ack(state, pending)
    assert not path.exists()
    with state._connect() as db:
        assert db.execute("SELECT status, payload_path FROM file_state").fetchone() == (
            "sent",
            str(path),
        )
        db.execute("DROP TRIGGER fail_clear")
    state = CollectorState(state.path, source_id="iphone_screen_time")
    state.cleanup_sent_payloads()
    with state._connect() as db:
        assert db.execute("SELECT status, payload_path FROM file_state").fetchone() == (
            "sent",
            None,
        )
