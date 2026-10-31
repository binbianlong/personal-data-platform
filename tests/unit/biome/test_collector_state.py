import gzip
from datetime import UTC, datetime

from personal_data_platform.biome.collector_state import CollectorState

NOW = datetime(2026, 8, 27, 1, 2, 3, tzinfo=UTC)


def test_raw_database_is_private(tmp_path):
    path = tmp_path / "state" / "collector.db"
    CollectorState(path, source_id="iphone_screen_time")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def _prepare(state: CollectorState, raw_bytes: bytes):
    return state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        raw_bytes=raw_bytes,
        observed_at=NOW,
    )


def test_pending_payload_survives_restart_and_loaded_latest_is_skipped(tmp_path) -> None:
    state_path = tmp_path / "collector.db"
    first_state = CollectorState(state_path, source_id="iphone_screen_time")
    first = _prepare(first_state, b"state-a")
    assert first is not None
    assert first.created

    restarted_state = CollectorState(state_path, source_id="iphone_screen_time")
    retried = _prepare(restarted_state, b"state-a")
    assert retried is not None
    assert not retried.created
    assert retried.identity.object_key == first.identity.object_key
    assert retried.compressed_payload == first.compressed_payload
    assert gzip.decompress(retried.compressed_payload) == b"state-a"

    restarted_state.mark_loaded(retried.identity.object_key, NOW)

    assert _prepare(restarted_state, b"state-a") is None
    assert restarted_state.pending() == []
