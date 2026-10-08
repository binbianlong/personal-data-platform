"""Decode Biome SEGB records and Screen Time event payloads."""

from __future__ import annotations

import hashlib
import struct
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from personal_data_platform.raw.models import RawObject

from .models import ParsedScreenTimeRecord, PayloadDecodeError, SegmentDecodeError
from .payloads import (
    CF_ABSOLUTE_TIME_EPOCH,
    DecodedTombstone,
    decode_app_in_focus_payload,
    decode_mac_app_usage_payload,
    decode_tombstone_payload,
)

PARSER_VERSION = "app-in-focus-v2"
MAC_APP_USAGE_PARSER_VERSION = "app-usage-v1"
UNIX_TO_CF_SECONDS = 978_307_200
EVENT_KEY_DOMAIN = b"screen-time/event/v1\0"


@dataclass(frozen=True, slots=True)
class DecodedScreenTimeEvent:
    """Normalized event fields shared by ingestion and stable event identities."""

    in_foreground: bool
    cf_absolute_time: float
    event_at: datetime
    bundle_id: str
    unknown_field_count: int
    transition_reason: str | None = None
    kind: int | None = None
    app_version: str | None = None
    app_build: str | None = None
    platform_flag: int | None = None


def _decode_app_in_focus_event(payload: bytes) -> DecodedScreenTimeEvent:
    return DecodedScreenTimeEvent(**decode_app_in_focus_payload(payload))


def _decode_mac_app_usage_event(payload: bytes) -> DecodedScreenTimeEvent:
    decoded = decode_mac_app_usage_payload(payload)
    return DecodedScreenTimeEvent(
        in_foreground=decoded["in_foreground"],
        # Preserve the original float conversion used by persisted event keys.
        cf_absolute_time=decoded["unix_time"] - UNIX_TO_CF_SECONDS,
        event_at=decoded["event_at"],
        bundle_id=decoded["bundle_id"],
        unknown_field_count=decoded["unknown_field_count"],
    )


class SegbRecord(Protocol):
    """Fields shared by ccl-segb v1 and v2 records; metadata is version-specific."""

    @property
    def data(self) -> bytes: ...

    @property
    def data_start_offset(self) -> int: ...


def event_key(
    *,
    device_key: str,
    stream: str,
    bundle_id: str,
    cf_absolute_time: float,
    in_foreground: bool,
    kind: int | None,
) -> str:
    """Build the stable cross-segment identity for one logical event."""

    strings = (device_key.encode(), stream.encode(), bundle_id.encode())
    canonical = b"".join(
        (
            EVENT_KEY_DOMAIN,
            *(struct.pack(">I", len(value)) + value for value in strings),
            struct.pack(">d", cf_absolute_time),
            struct.pack(">I", int(in_foreground)),
            struct.pack(">I", 0xFFFFFFFF if kind is None else kind),
        )
    )
    return hashlib.sha256(canonical).hexdigest()


def _record_timestamp(record: SegbRecord) -> datetime | None:
    timestamp = getattr(record, "timestamp1", None)
    if timestamp is None:
        metadata = getattr(record, "metadata", None)
        timestamp = getattr(metadata, "creation", None)
    if timestamp is None:
        return None
    if isinstance(timestamp, datetime):
        return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp, tz=UTC)
    return None


def parse_segb_records(
    raw: RawObject,
    segment: bytes,
    records: Iterable[SegbRecord],
    *,
    segment_kind: str | None = None,
) -> list[ParsedScreenTimeRecord]:
    """Keep deletion/CRC metadata without requiring a surviving event payload."""
    if raw.stream == "app-usage":
        decode_event = _decode_mac_app_usage_event
        parser_version = MAC_APP_USAGE_PARSER_VERSION
    else:
        decode_event = _decode_app_in_focus_event
        parser_version = PARSER_VERSION
    parsed: list[ParsedScreenTimeRecord] = []
    for record in records:
        payload = bytes(record.data)
        state = getattr(record, "state", "UNKNOWN")
        state_name = getattr(state, "name", str(state)).upper()
        record_offset = int(record.data_start_offset)
        metadata = getattr(record, "metadata", None)
        metadata_offset = int(getattr(metadata, "metadata_offset", record_offset))
        timestamp = _record_timestamp(record)
        timestamp_cocoa = (
            (timestamp - CF_ABSOLUTE_TIME_EPOCH).total_seconds() if timestamp else None
        )
        if segment.startswith(b"SEGB") and metadata is not None:
            if metadata_offset < 32 or metadata_offset + 16 > len(segment):
                raise SegmentDecodeError("SEGB metadata offset is outside the source bytes")
            timestamp_cocoa = struct.unpack_from("<d", segment, metadata_offset + 8)[0]
        crc = getattr(record, "crc_passed", None)
        decoded: DecodedScreenTimeEvent | None = None
        tombstone: DecodedTombstone | None = None
        identity = None
        if state_name == "DELETED":
            kind = "deleted"
            if crc is not False:
                try:
                    decoded = decode_event(payload)
                except PayloadDecodeError:
                    pass
        elif state_name != "WRITTEN":
            raise SegmentDecodeError("unsupported SEGB record state")
        elif crc is False:
            kind = "crc_failure"
        else:
            tombstone = decode_tombstone_payload(payload)
            if tombstone:
                if segment_kind == "events":
                    raise PayloadDecodeError("tombstone payload in an events segment")
                kind = "tombstone"
            else:
                if segment_kind == "tombstones":
                    raise PayloadDecodeError("unrecognized tombstone payload")
                kind = "event"
                decoded = decode_event(payload)
                identity = event_key(
                    device_key=raw.subject_key,
                    stream=raw.stream,
                    bundle_id=decoded.bundle_id,
                    cf_absolute_time=decoded.cf_absolute_time,
                    in_foreground=decoded.in_foreground,
                    kind=decoded.kind,
                )
        parsed.append(
            ParsedScreenTimeRecord(
                event_key=identity,
                object_key=raw.key,
                device_key=raw.subject_key,
                source_stream=raw.stream,
                segment_key=raw.logical_key,
                segment_sha256=raw.sha256,
                observed_at=raw.observed_at,
                segment_filename=Path(raw.key).name.removesuffix(".gz"),
                record_offset=record_offset,
                record_metadata_offset=metadata_offset,
                record_state=state_name,
                segment_record_timestamp=timestamp,
                crc_passed=crc,
                transition_reason=decoded.transition_reason if decoded is not None else None,
                kind=decoded.kind if decoded is not None else None,
                in_foreground=decoded.in_foreground if decoded is not None else None,
                cf_absolute_time=decoded.cf_absolute_time if decoded is not None else None,
                event_at=decoded.event_at if decoded is not None else None,
                bundle_id=decoded.bundle_id if decoded is not None else None,
                app_version=decoded.app_version if decoded is not None else None,
                app_build=decoded.app_build if decoded is not None else None,
                platform_flag=decoded.platform_flag if decoded is not None else None,
                unknown_field_count=decoded.unknown_field_count if decoded is not None else 0,
                original_payload=payload,
                parser_version=parser_version,
                record_kind=kind,
                payload_length=len(payload),
                record_timestamp_cocoa=timestamp_cocoa,
                target_segment_name=tombstone["target_segment_name"] if tombstone else None,
                target_offset=tombstone["target_offset"] if tombstone else None,
                target_length=tombstone["target_length"] if tombstone else None,
                target_event_timestamp=tombstone["target_event_timestamp"] if tombstone else None,
                deletion_reason=tombstone["deletion_reason"] if tombstone else None,
            )
        )
    return parsed


def _validate_segb2_record_states(segment: bytes) -> None:
    """Reject unknown states before ccl-segb can silently discard their trailers."""
    if not segment.startswith(b"SEGB"):
        return  # SEGB v1 rejects unknown states in the dependency itself.
    if len(segment) < 32:
        raise SegmentDecodeError("truncated SEGB header")
    entry_count = struct.unpack_from("<i", segment, 4)[0]
    trailer_start = len(segment) - 16 * entry_count
    if entry_count < 0 or trailer_start < 32:
        raise SegmentDecodeError("SEGB trailer is outside the source bytes")
    for offset in range(trailer_start, len(segment), 16):
        end_offset, state = struct.unpack_from("<2i", segment, offset)
        # Zeroed unused slots reference no data; state 4 is a known empty record.
        if state == 0 and end_offset == 0:
            continue
        if state not in {1, 3, 4}:
            raise SegmentDecodeError(
                f"unsupported SEGB record state {state} at metadata offset {offset}"
            )


def _read_segb_records(segment: bytes) -> list[SegbRecord]:
    """Expose immutable bytes privately to the existing path-based decoder."""
    try:
        from ccl_segb import read_segb_file
    except ImportError as error:  # pragma: no cover - packaging failure
        raise SegmentDecodeError(
            "ccl-segb is required to decode raw Screen Time objects"
        ) from error

    try:
        _validate_segb2_record_states(segment)
        with tempfile.NamedTemporaryFile(suffix=".segb") as temporary:
            temporary.write(segment)
            temporary.flush()
            records: list[SegbRecord] = list(read_segb_file(temporary.name))
        return records
    except Exception as error:
        raise SegmentDecodeError(f"failed to decode SEGB: {error}") from error


def validate_segb_snapshot(segment: bytes) -> None:
    """Reject partial containers/CRCs without requiring a known application schema."""
    if segment.startswith(b"SEGB") and len(segment) >= 32:
        count = struct.unpack_from("<i", segment, 4)[0]
        trailer = len(segment) - 16 * count
        if count < 0 or trailer < 32:
            raise SegmentDecodeError("incomplete SEGB trailer")
        for offset in range(trailer, len(segment), 16):
            end, state = struct.unpack_from("<2i", segment, offset)
            if state in {1, 3} and (end < 8 or 32 + end > trailer):
                raise SegmentDecodeError("incomplete SEGB record")
    for record in _read_segb_records(segment):
        state = getattr(record, "state", None)
        if (
            getattr(state, "name", "").upper() == "WRITTEN"
            and getattr(record, "crc_passed", None) is False
        ):
            raise SegmentDecodeError("snapshot has an incomplete record CRC")


def parse_segb_bytes(
    raw: RawObject, segment: bytes, *, segment_kind: str | None = None
) -> list[ParsedScreenTimeRecord]:
    """Decode one complete uncompressed SEGB object and its application payloads."""
    try:
        return parse_segb_records(
            raw, segment, _read_segb_records(segment), segment_kind=segment_kind
        )
    except PayloadDecodeError:
        raise
    except Exception as error:
        raise SegmentDecodeError(f"failed to decode {raw.key}: {error}") from error
