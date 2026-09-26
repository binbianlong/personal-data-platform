"""Decode Biome SEGB records and Screen Time event payloads."""

from __future__ import annotations

import hashlib
import math
import struct
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypedDict

from personal_data_platform.raw.models import RawObject

from .models import ParsedScreenTimeRecord, PayloadDecodeError, SegmentDecodeError

PARSER_VERSION = "app-in-focus-v2"
MAC_APP_USAGE_PARSER_VERSION = "app-usage-v1"
CF_ABSOLUTE_TIME_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
UNIX_TO_CF_SECONDS = 978_307_200
EVENT_KEY_DOMAIN = b"screen-time/event/v1\0"


class DecodedAppInFocus(TypedDict):
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


class DecodedTombstone(TypedDict):
    target_segment_name: str
    target_offset: int | None
    target_length: int | None
    deletion_reason: int | None
    target_event_timestamp: float


class SegbRecord(Protocol):
    """Fields shared by ccl-segb v1 and v2 records; metadata is version-specific."""

    @property
    def data(self) -> bytes: ...

    @property
    def data_start_offset(self) -> int: ...


@dataclass(frozen=True, slots=True)
class _WireValue:
    field_number: int
    wire_type: int
    value: int | bytes


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(payload) and shift < 70:
        current = payload[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if current & 0x80 == 0:
            return value, offset
        shift += 7
    raise PayloadDecodeError("truncated or overlong protobuf varint")


def _decode_wire(payload: bytes) -> list[_WireValue]:
    values: list[_WireValue] = []
    offset = 0
    value: int | bytes
    while offset < len(payload):
        tag, offset = _read_varint(payload, offset)
        field_number, wire_type = tag >> 3, tag & 0x07
        if field_number == 0:
            raise PayloadDecodeError("protobuf field number 0 is invalid")
        if wire_type == 0:
            value, offset = _read_varint(payload, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(payload):
                raise PayloadDecodeError("truncated protobuf fixed64")
            value, offset = payload[offset:end], end
        elif wire_type == 2:
            size, offset = _read_varint(payload, offset)
            end = offset + size
            if end > len(payload):
                raise PayloadDecodeError("truncated protobuf length-delimited value")
            value, offset = payload[offset:end], end
        elif wire_type == 5:
            end = offset + 4
            if end > len(payload):
                raise PayloadDecodeError("truncated protobuf fixed32")
            value, offset = payload[offset:end], end
        else:
            raise PayloadDecodeError(f"unsupported protobuf wire type: {wire_type}")
        values.append(_WireValue(field_number, wire_type, value))
    return values


def _one(values: list[_WireValue], field: int, wire_type: int) -> int | bytes | None:
    matches = [value.value for value in values if value.field_number == field]
    if not matches:
        return None
    if len(matches) != 1:
        raise PayloadDecodeError(f"protobuf field {field} occurred more than once")
    match = next(value for value in values if value.field_number == field)
    if match.wire_type != wire_type:
        raise PayloadDecodeError(
            f"protobuf field {field} has wire type {match.wire_type}, expected {wire_type}"
        )
    return matches[0]


def _utf8(value: int | bytes | None, field: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, bytes):
        raise PayloadDecodeError(f"protobuf field {field} is not bytes")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PayloadDecodeError(f"protobuf field {field} is not UTF-8") from error


def _uint(value: int | bytes | None, field: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise PayloadDecodeError(f"protobuf field {field} is not an integer")
    if not 0 <= value <= 0xFFFFFFFF:
        raise PayloadDecodeError(f"protobuf field {field} is outside uint32")
    return value


def decode_app_in_focus_payload(payload: bytes) -> DecodedAppInFocus:
    """Decode the observed fields of an iPhone ``App.InFocus`` payload.

    Unknown fields are counted and retained in the original payload instead of
    making a forward-compatible schema extension fatal.
    """

    values = _decode_wire(payload)
    transition_reason = _utf8(_one(values, 1, 2), 1)
    kind = _uint(_one(values, 2, 0), 2)
    in_foreground_raw = _uint(_one(values, 3, 0), 3)
    time_raw = _one(values, 4, 1)
    bundle_id = _utf8(_one(values, 6, 2), 6)
    app_version = _utf8(_one(values, 9, 2), 9)
    app_build = _utf8(_one(values, 10, 2), 10)
    platform_flag = _uint(_one(values, 13, 0), 13)

    if in_foreground_raw not in {0, 1}:
        raise PayloadDecodeError("protobuf field 3 must be 0 or 1")
    if not isinstance(time_raw, bytes):
        raise PayloadDecodeError("protobuf field 4 is required")
    cf_absolute_time = struct.unpack("<d", time_raw)[0]
    if not math.isfinite(cf_absolute_time):
        raise PayloadDecodeError("protobuf field 4 must be a finite double")
    if bundle_id is None or not bundle_id.strip():
        raise PayloadDecodeError("protobuf field 6 is required")

    try:
        event_at = CF_ABSOLUTE_TIME_EPOCH + timedelta(seconds=cf_absolute_time)
    except OverflowError as error:
        raise PayloadDecodeError("protobuf field 4 is outside the datetime range") from error

    known_fields = {1, 2, 3, 4, 6, 9, 10, 13}
    return {
        "transition_reason": transition_reason,
        "kind": kind,
        "in_foreground": bool(in_foreground_raw),
        "cf_absolute_time": cf_absolute_time,
        "event_at": event_at,
        "bundle_id": bundle_id,
        "app_version": app_version,
        "app_build": app_build,
        "platform_flag": platform_flag,
        "unknown_field_count": sum(value.field_number not in known_fields for value in values),
    }


def decode_mac_app_usage_payload(payload: bytes) -> DecodedAppInFocus:
    """Decode observed Mac AppUsage start/end records with Unix-second timestamps."""
    values = _decode_wire(payload)
    in_foreground_raw = _uint(_one(values, 1, 0), 1)
    time_raw = _one(values, 2, 1)
    bundle_id = _utf8(_one(values, 3, 2), 3)
    if in_foreground_raw not in {0, 1}:
        raise PayloadDecodeError("protobuf field 1 must be 0 or 1")
    if not isinstance(time_raw, bytes):
        raise PayloadDecodeError("protobuf field 2 is required")
    unix_time = struct.unpack("<d", time_raw)[0]
    if not math.isfinite(unix_time):
        raise PayloadDecodeError("protobuf field 2 must be a finite double")
    if bundle_id is None or not bundle_id.strip():
        raise PayloadDecodeError("protobuf field 3 is required")
    try:
        event_at = datetime.fromtimestamp(unix_time, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise PayloadDecodeError("protobuf field 2 is outside the datetime range") from error
    return {
        "transition_reason": None,
        "kind": None,
        "in_foreground": bool(in_foreground_raw),
        "cf_absolute_time": unix_time - UNIX_TO_CF_SECONDS,
        "event_at": event_at,
        "bundle_id": bundle_id,
        "app_version": None,
        "app_build": None,
        "platform_flag": None,
        "unknown_field_count": sum(value.field_number not in {1, 2, 3} for value in values),
    }


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


def decode_tombstone_payload(payload: bytes) -> DecodedTombstone | None:
    """Recognize BMTombstoneEvent, not an incompatible App.InFocus event.

    Tags verified with macOS BMTombstoneEvent.initWithProtoData/jsonDictionary:
    1 segmentName, 2 offset, 3 length, 4 reason, 5 processName,
    6 eventTimestamp, 7 policyID. Reasons: 1 TTL, 2 UserInitiated.
    """
    values = _decode_wire(payload)
    signature = {(v.field_number, v.wire_type) for v in values}
    if not {(1, 2), (2, 0), (3, 0), (4, 0), (5, 2), (6, 1)} <= signature:
        return None
    name = _utf8(_one(values, 1, 2), 1)
    if not name or not name.isascii() or not name.isdecimal():
        raise PayloadDecodeError("invalid tombstone segment name")
    time_raw = _one(values, 6, 1)
    # The signature and _one wire-type validation above guarantee fixed64 bytes.
    assert isinstance(time_raw, bytes)
    timestamp = struct.unpack("<d", time_raw)[0]
    if not math.isfinite(timestamp):
        raise PayloadDecodeError("invalid tombstone event timestamp")
    _utf8(_one(values, 5, 2), 5)
    _utf8(_one(values, 7, 2), 7)
    return {
        "target_segment_name": name,
        "target_offset": _uint(_one(values, 2, 0), 2),
        "target_length": _uint(_one(values, 3, 0), 3),
        "deletion_reason": _uint(_one(values, 4, 0), 4),
        "target_event_timestamp": timestamp,
    }


def parse_segb_records(
    raw: RawObject,
    segment: bytes,
    records: Iterable[SegbRecord],
    *,
    segment_kind: str | None = None,
) -> list[ParsedScreenTimeRecord]:
    """Keep deletion/CRC metadata without requiring a surviving event payload."""
    if raw.stream == "app-usage":
        decode_event = decode_mac_app_usage_payload
        parser_version = MAC_APP_USAGE_PARSER_VERSION
    else:
        decode_event = decode_app_in_focus_payload
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
        decoded: DecodedAppInFocus | None = None
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
                    bundle_id=decoded["bundle_id"],
                    cf_absolute_time=decoded["cf_absolute_time"],
                    in_foreground=decoded["in_foreground"],
                    kind=decoded["kind"],
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
                transition_reason=decoded["transition_reason"] if decoded is not None else None,
                kind=decoded["kind"] if decoded is not None else None,
                in_foreground=decoded["in_foreground"] if decoded is not None else None,
                cf_absolute_time=decoded["cf_absolute_time"] if decoded is not None else None,
                event_at=decoded["event_at"] if decoded is not None else None,
                bundle_id=decoded["bundle_id"] if decoded is not None else None,
                app_version=decoded["app_version"] if decoded is not None else None,
                app_build=decoded["app_build"] if decoded is not None else None,
                platform_flag=decoded["platform_flag"] if decoded is not None else None,
                unknown_field_count=decoded["unknown_field_count"] if decoded is not None else 0,
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


def parse_segb_bytes(
    raw: RawObject, segment: bytes, *, segment_kind: str | None = None
) -> list[ParsedScreenTimeRecord]:
    """Decode one complete uncompressed SEGB object.

    ``ccl-segb`` currently accepts paths, so the immutable bytes are exposed
    through a private temporary file for the duration of parsing.
    """

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
        return parse_segb_records(raw, segment, records, segment_kind=segment_kind)
    except PayloadDecodeError:
        raise
    except Exception as error:
        raise SegmentDecodeError(f"failed to decode {raw.key}: {error}") from error
