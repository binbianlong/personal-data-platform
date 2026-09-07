"""Decoded Screen Time records and source payload errors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from personal_data_platform.loader.models import RawDecodeError


@dataclass(frozen=True, slots=True)
class ParsedScreenTimeRecord:
    """One decoded record occurrence within a raw segment observation."""

    event_key: str
    object_key: str
    device_key: str
    source_stream: str
    segment_key: str
    segment_sha256: str
    observed_at: datetime
    segment_filename: str
    record_offset: int
    record_metadata_offset: int
    record_state: str
    segment_record_timestamp: datetime | None
    crc_passed: bool | None
    transition_reason: str | None
    kind: int | None
    in_foreground: bool
    cf_absolute_time: float
    event_at: datetime
    bundle_id: str
    app_version: str | None
    app_build: str | None
    platform_flag: int | None
    unknown_field_count: int
    original_payload: bytes
    parser_version: str


class SegmentDecodeError(RawDecodeError):
    """Raised when a SEGB observation cannot be decoded atomically."""


class PayloadDecodeError(RawDecodeError):
    """Raised when an App.InFocus payload violates the supported wire contract."""
