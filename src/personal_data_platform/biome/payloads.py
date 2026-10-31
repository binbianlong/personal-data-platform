"""Format-specific protobuf decoding for Screen Time events and tombstones."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import PayloadDecodeError

CF_ABSOLUTE_TIME_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


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


def is_tombstone_payload(payload: bytes) -> bool:
    """Recognize BMTombstoneEvent for local format diagnostics."""
    values = _decode_wire(payload)
    signature = {(v.field_number, v.wire_type) for v in values}
    if not {(1, 2), (2, 0), (3, 0), (4, 0), (5, 2), (6, 1)} <= signature:
        return False
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
    return True
