import gzip
from datetime import UTC, datetime

import pytest

from personal_data_platform.raw.screen_time import (
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
    assert parsed.device_key == identity.device_key
    assert parsed.stream == identity.stream
    assert parsed.segment_key == identity.segment_key
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
