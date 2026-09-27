"""Format-specific protobuf decoding for Screen Time events and tombstones."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from .models import PayloadDecodeError

CF_ABSOLUTE_TIME_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


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


class DecodedMacAppUsage(TypedDict):
    in_foreground: bool
    unix_time: float
    event_at: datetime
    bundle_id: str
    unknown_field_count: int


class DecodedTombstone(TypedDict):
    target_segment_name: str
    target_offset: int | None
    target_length: int | None
    deletion_reason: int | None
    target_event_timestamp: float


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


def decode_mac_app_usage_payload(payload: bytes) -> DecodedMacAppUsage:
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
        "in_foreground": bool(in_foreground_raw),
        "unix_time": unix_time,
        "event_at": event_at,
        "bundle_id": bundle_id,
        "unknown_field_count": sum(value.field_number not in {1, 2, 3} for value in values),
    }


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
