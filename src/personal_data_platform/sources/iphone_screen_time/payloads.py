"""Source payload wire decoding."""

from __future__ import annotations

import math
import struct
from datetime import datetime, timedelta
from typing import TypedDict

from personal_data_platform.biome.models import PayloadDecodeError
from personal_data_platform.biome.payloads import (
    CF_ABSOLUTE_TIME_EPOCH,
    _decode_wire,
    _one,
    _uint,
    _utf8,
)


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
