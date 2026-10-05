import gzip
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.screen_time.state import CollectorState, SuccessfulScan

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


def test_pending_recovery_keeps_original_v1_and_v2_identities(tmp_path) -> None:
    from personal_data_platform.sources.screen_time.raw import encode_segment_envelope

    state = CollectorState(tmp_path / "state.db")
    first = _prepare(state, b"legacy")
    second = state.prepare(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        raw_bytes=encode_segment_envelope(b"segb", name="123", kind="events"),
        observed_at=NOW,
        schema_version=2,
    )
    restarted = CollectorState(tmp_path / "state.db")
    pending = restarted.pending()
    assert [p.identity for p in pending] == [first.identity, second.identity]
    assert [p.identity.schema_version for p in pending] == [1, 2]
    assert [p.compressed_payload for p in pending] == [
        first.compressed_payload,
        second.compressed_payload,
    ]


@pytest.mark.parametrize("stream", ["app-in-focus", "app-usage"])
@pytest.mark.parametrize("control_kind", ["receipt", "manifest"])
def test_control_is_due_at_24_hours_and_on_destination_change(
    tmp_path, stream, control_kind
) -> None:
    path = tmp_path / "collector.db"
    state = CollectorState(path)
    control = dict(
        stream=stream,
        device_key="a" * 64 if control_kind == "receipt" else "",
        destination="old-bucket",
        config_digest="active-config",
        control_kind=control_kind,
    )
    assert state.control_due(**control, now=NOW)
    state.mark_control_published(**control, published_at=NOW)
    restarted = CollectorState(path)
    assert not restarted.control_due(**control, now=NOW + timedelta(hours=23, minutes=59))
    assert restarted.control_due(**control, now=NOW + timedelta(hours=24))
    changed = control | {"destination": "new-bucket"}
    assert restarted.control_due(**changed, now=NOW + timedelta(minutes=30))
    restarted.mark_control_published(**changed, published_at=NOW + timedelta(minutes=30))
    # Returning to an earlier destination also needs a fresh publication.
    assert restarted.control_due(**control, now=NOW + timedelta(hours=1))


def test_configuration_change_invalidates_both_controls_without_losing_pending(tmp_path) -> None:
    state = CollectorState(tmp_path / "collector.db")
    pending = _prepare(state, b"durable")
    common = dict(stream="app-in-focus", destination="bucket", config_digest="active")
    receipt = dict(device_key="a" * 64, control_kind="receipt")
    manifest = dict(device_key="", control_kind="manifest")
    for kind in (receipt, manifest):
        state.mark_control_published(**common, **kind, published_at=NOW)
    assert not state.control_due(**common, **receipt, now=NOW)

    inactive = common | {"config_digest": "inactive"}
    assert state.control_due(**inactive, **manifest, now=NOW)
    state.mark_control_published(**inactive, **manifest, published_at=NOW)
    restarted = CollectorState(state.path)
    assert restarted.control_due(**common, **receipt, now=NOW)
    assert restarted.control_due(**common, **manifest, now=NOW)
    assert restarted.pending()[0].identity == pending.identity
    assert restarted.pending()[0].compressed_payload == pending.compressed_payload
