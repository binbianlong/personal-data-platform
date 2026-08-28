"""Decode Biome SEGB records and iPhone ``App.InFocus`` protobuf payloads."""

from __future__ import annotations

import hashlib
import math
import struct
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import ParsedScreenTimeRecord, PayloadDecodeError, RawObject, SegmentDecodeError

PARSER_VERSION = "app-in-focus-v1"
CF_ABSOLUTE_TIME_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
EVENT_KEY_DOMAIN = b"screen-time/event/v1\0"


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


def decode_app_in_focus_payload(payload: bytes) -> dict[str, Any]:
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


def _record_timestamp(record: Any) -> datetime | None:
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
    raw: RawObject, segment: bytes, records: Iterable[Any]
) -> list[ParsedScreenTimeRecord]:
    """Normalize decoded ccl-segb records for one immutable raw object."""

    parsed: list[ParsedScreenTimeRecord] = []
    for record in records:
        payload = bytes(record.data)
        decoded = decode_app_in_focus_payload(payload)
        state = getattr(record, "state", "UNKNOWN")
        state_name = getattr(state, "name", str(state)).upper()
        record_offset = int(getattr(record, "data_start_offset"))
        metadata = getattr(record, "metadata", None)
        record_metadata_offset = int(getattr(metadata, "metadata_offset", record_offset))
        parsed.append(
            ParsedScreenTimeRecord(
                event_key=event_key(
                    device_key=raw.device_key,
                    stream=raw.stream,
                    bundle_id=decoded["bundle_id"],
                    cf_absolute_time=decoded["cf_absolute_time"],
                    in_foreground=decoded["in_foreground"],
                    kind=decoded["kind"],
                ),
                object_key=raw.key,
                device_key=raw.device_key,
                source_stream=raw.stream,
                segment_key=raw.segment_key,
                segment_sha256=raw.sha256,
                observed_at=raw.observed_at,
                segment_filename=Path(raw.key).name.removesuffix(".gz"),
                record_offset=record_offset,
                record_metadata_offset=record_metadata_offset,
                record_state=state_name,
                segment_record_timestamp=_record_timestamp(record),
                crc_passed=getattr(record, "crc_passed", None),
                transition_reason=decoded["transition_reason"],
                kind=decoded["kind"],
                in_foreground=decoded["in_foreground"],
                cf_absolute_time=decoded["cf_absolute_time"],
                event_at=decoded["event_at"],
                bundle_id=decoded["bundle_id"],
                app_version=decoded["app_version"],
                app_build=decoded["app_build"],
                platform_flag=decoded["platform_flag"],
                unknown_field_count=decoded["unknown_field_count"],
                original_payload=payload,
                parser_version=PARSER_VERSION,
            )
        )
    return parsed


def parse_segb_bytes(raw: RawObject, segment: bytes) -> list[ParsedScreenTimeRecord]:
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
        with tempfile.NamedTemporaryFile(suffix=".segb") as temporary:
            temporary.write(segment)
            temporary.flush()
            records = list(read_segb_file(temporary.name))
        return parse_segb_records(raw, segment, records)
    except PayloadDecodeError:
        raise
    except Exception as error:
        raise SegmentDecodeError(f"failed to decode {raw.key}: {error}") from error
