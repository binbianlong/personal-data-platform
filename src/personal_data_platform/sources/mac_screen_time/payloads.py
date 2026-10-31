"""Source payload wire decoding."""

from __future__ import annotations

import math
import struct
from datetime import UTC, datetime
from typing import TypedDict

from personal_data_platform.biome.models import PayloadDecodeError
from personal_data_platform.biome.payloads import (
    _decode_wire,
    _one,
    _uint,
    _utf8,
)


class DecodedMacAppUsage(TypedDict):
    in_foreground: bool
    unix_time: float
    event_at: datetime
    bundle_id: str
    unknown_field_count: int


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
