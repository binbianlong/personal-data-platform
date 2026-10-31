"""Immutable Raw identity for this source."""

from datetime import datetime

from personal_data_platform.biome.raw import (
    RAW_SCHEMA_VERSION,
    build_device_key,
    build_segment_key,
    decode_segment_envelope,
    encode_segment_envelope,
    format_observed_at,
    gzip_raw_bytes,
    parse_observed_at,
    sha256_hex,
)
from personal_data_platform.biome.raw import (
    ScreenTimeRawIdentity as _Identity,
)
from personal_data_platform.biome.raw import (
    parse_raw_object_key as _parse_key,
)
from personal_data_platform.biome.raw import (
    validate_raw_object_key as _validate_key,
)
from personal_data_platform.raw.models import RawObject

SOURCE_ID = "iphone_screen_time"
APP_IN_FOCUS_STREAM = "app-in-focus"
RAW_PREFIX = "raw/iphone_screen_time/v2"


def ScreenTimeRawIdentity(
    *,
    device_key: str,
    stream: str,
    segment_key: str,
    observed_at: datetime,
    sha256: str,
) -> _Identity:
    return _Identity(
        source_id=SOURCE_ID,
        device_key=device_key,
        stream=stream,
        segment_key=segment_key,
        observed_at=observed_at,
        sha256=sha256,
    )


def parse_raw_object_key(
    key: str, *, storage_created_at: datetime, storage_generation: int
) -> RawObject:
    return _parse_key(
        key,
        source_id=SOURCE_ID,
        storage_created_at=storage_created_at,
        storage_generation=storage_generation,
    )


def validate_raw_object_key(key: str) -> None:
    _validate_key(key, source_id=SOURCE_ID)
    if key.split("/")[4] != APP_IN_FOCUS_STREAM:
        raise ValueError("unsupported source stream")


__all__ = [
    "RAW_SCHEMA_VERSION",
    "build_device_key",
    "build_segment_key",
    "decode_segment_envelope",
    "encode_segment_envelope",
    "format_observed_at",
    "parse_observed_at",
    "gzip_raw_bytes",
    "sha256_hex",
    "ScreenTimeRawIdentity",
    "parse_raw_object_key",
    "validate_raw_object_key",
]
