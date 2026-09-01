"""Data types shared by the raw loader."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RawObject:
    """One immutable Screen Time segment observation stored in object storage."""

    key: str
    device_key: str
    stream: str
    segment_key: str
    observed_at: datetime
    sha256: str
    storage_created_at: datetime
    storage_generation: int


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


@dataclass(frozen=True, slots=True)
class LoadSummary:
    """Outcome of a loader run."""

    discovered: int
    skipped: int
    succeeded: int
    failed: int
    records: int

    @property
    def ok(self) -> bool:
        return self.failed == 0


class SegmentDecodeError(ValueError):
    """Raised when a raw segment cannot be decoded atomically."""


class PayloadDecodeError(ValueError):
    """Raised when an App.InFocus payload violates the supported wire contract."""
