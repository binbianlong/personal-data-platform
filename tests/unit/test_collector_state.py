import gzip
from datetime import UTC, datetime

from personal_data_platform.collectors.state import CollectorState, SuccessfulScan

NOW = datetime(2026, 8, 27, 1, 2, 3, tzinfo=UTC)


def _prepare(state: CollectorState, raw_bytes: bytes):
    return state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        raw_bytes=raw_bytes,
        observed_at=NOW,
    )


def test_pending_payload_survives_restart_and_uploaded_latest_is_skipped(tmp_path) -> None:
    state_path = tmp_path / "collector.db"
    first_state = CollectorState(state_path)
    first = _prepare(first_state, b"state-a")
    assert first is not None
    assert first.created

    restarted_state = CollectorState(state_path)
    retried = _prepare(restarted_state, b"state-a")
    assert retried is not None
    assert not retried.created
    assert retried.identity.object_key == first.identity.object_key
    assert retried.compressed_payload == first.compressed_payload
    assert gzip.decompress(retried.compressed_payload) == b"state-a"

    restarted_state.mark_uploaded(retried.identity.object_key, NOW)

    assert _prepare(restarted_state, b"state-a") is None
    assert restarted_state.pending() == []


def test_return_to_old_content_is_a_new_observation(tmp_path) -> None:
    state = CollectorState(tmp_path / "collector.db")
    keys = []

    for raw_bytes in (b"state-a", b"state-b", b"state-a"):
        pending = _prepare(state, raw_bytes)
        assert pending is not None
        keys.append(pending.identity.object_key)
        state.mark_uploaded(pending.identity.object_key, NOW)

    assert len(set(keys)) == 3
    assert keys[0].rsplit("/", 1)[1] == keys[2].rsplit("/", 1)[1]
    assert keys[0] != keys[2]


def test_successful_scan_is_updated_only_when_explicitly_recorded(tmp_path) -> None:
    state = CollectorState(tmp_path / "collector.db")
    assert state.last_successful_scan() is None
    scan = SuccessfulScan(
        completed_at=NOW,
        device_count=1,
        segment_count=4,
        uploaded_count=2,
        skipped_count=2,
    )

    state.record_successful_scan(scan)

    assert state.last_successful_scan() == scan
