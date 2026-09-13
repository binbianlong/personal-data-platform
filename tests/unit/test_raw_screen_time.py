import gzip
from datetime import UTC, datetime

import pytest

from personal_data_platform.sources.screen_time.raw import (
    APP_IN_FOCUS_STREAM,
    ScreenTimeRawIdentity,
    build_device_key,
    build_segment_key,
    gzip_raw_bytes,
    parse_raw_object_key,
    sha256_hex,
)

SECRET = bytes.fromhex("11" * 32)
OBSERVED_AT = datetime(2026, 8, 27, 1, 2, 3, 456789, tzinfo=UTC)


def test_raw_identity_uses_hmac_keys_and_hashes_uncompressed_bytes() -> None:
    raw_bytes = b"synthetic-segb-bytes"
    device_key = build_device_key(SECRET, "synthetic-device")
    segment_key = build_segment_key(
        SECRET,
        device_identifier="synthetic-device",
        stream="App.InFocus",
        relative_path="segment-001",
    )
    identity = ScreenTimeRawIdentity(
        device_key=device_key,
        stream=APP_IN_FOCUS_STREAM,
        segment_key=segment_key,
        observed_at=OBSERVED_AT,
        sha256=sha256_hex(raw_bytes),
    )

    assert identity.object_key == (
        f"raw/screen_time/v1/{device_key}/app-in-focus/{segment_key}/"
        f"20260827T010203456789Z/{sha256_hex(raw_bytes)}.segb.gz"
    )
    assert "synthetic-device" not in identity.object_key
    assert gzip.decompress(gzip_raw_bytes(raw_bytes)) == raw_bytes
    assert gzip_raw_bytes(raw_bytes) == gzip_raw_bytes(raw_bytes)


def test_segment_hmac_is_scoped_by_device_stream_and_relative_path() -> None:
    baseline = build_segment_key(
        SECRET,
        device_identifier="device-a",
        stream="App.InFocus",
        relative_path="segment-001",
    )

    assert baseline != build_device_key(SECRET, "device-a")
    assert baseline != build_segment_key(
        SECRET,
        device_identifier="device-b",
        stream="App.InFocus",
        relative_path="segment-001",
    )
    assert baseline != build_segment_key(
        SECRET,
        device_identifier="device-a",
        stream="Other.Stream",
        relative_path="segment-001",
    )


def test_parse_raw_object_key_round_trips_identity() -> None:
    identity = ScreenTimeRawIdentity(
        device_key="a" * 64,
        stream=APP_IN_FOCUS_STREAM,
        segment_key="b" * 64,
        observed_at=OBSERVED_AT,
        sha256="c" * 64,
    )

    parsed = parse_raw_object_key(
        identity.object_key,
        storage_created_at=OBSERVED_AT,
        storage_generation=7,
    )

    assert parsed.key == identity.object_key
    assert parsed.subject_key == identity.device_key
    assert parsed.stream == identity.stream
    assert parsed.logical_key == identity.segment_key
    assert parsed.observed_at == identity.observed_at
    assert parsed.sha256 == identity.sha256
    assert parsed.storage_created_at == OBSERVED_AT
    assert parsed.storage_generation == 7


@pytest.mark.parametrize(
    "key",
    [
        "raw/screen_time/v2/" + "a" * 64,
        "raw/screen_time/v1/not-a-hmac/app-in-focus/segment/time/hash.segb.gz",
        (
            "raw/screen_time/v1/"
            + "a" * 64
            + "/App.InFocus/"
            + "b" * 64
            + "/20260827T010203456789Z/"
            + "c" * 64
            + ".segb.gz"
        ),
    ],
)
def test_parse_raw_object_key_rejects_noncanonical_keys(key: str) -> None:
    with pytest.raises(ValueError, match="invalid Screen Time Raw object key"):
        parse_raw_object_key(key, storage_created_at=OBSERVED_AT, storage_generation=1)


def test_raw_identity_rejects_naive_observation_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ScreenTimeRawIdentity(
            device_key="a" * 64,
            stream=APP_IN_FOCUS_STREAM,
            segment_key="b" * 64,
            observed_at=datetime(2026, 8, 27),
            sha256="c" * 64,
        )


def test_v2_envelope_preserves_source_bytes_and_round_trips_key() -> None:
    from personal_data_platform.sources.screen_time.raw import (
        decode_segment_envelope,
        encode_segment_envelope,
    )

    payload = encode_segment_envelope(b"SEGB-source-bytes", name="123", kind="events")
    assert decode_segment_envelope(payload) == (b"SEGB-source-bytes", "123", "events")
    identity = ScreenTimeRawIdentity(
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        observed_at=datetime(2026, 9, 13, tzinfo=UTC),
        sha256=sha256_hex(payload),
        schema_version=2,
    )
    parsed = parse_raw_object_key(
        identity.object_key, storage_created_at=identity.observed_at, storage_generation=1
    )
    assert parsed.schema_version == 2
    assert parsed.sha256 == sha256_hex(payload)


@pytest.mark.parametrize("payload", [b"", b"SEGB", b"PDPST\x02\0\0\0\xff{}"])
def test_v2_rejects_truncated_envelopes(payload) -> None:
    from personal_data_platform.sources.screen_time.raw import decode_segment_envelope

    with pytest.raises(ValueError):
        decode_segment_envelope(payload)
