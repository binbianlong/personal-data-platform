"""Canonical OwnTracks location messages and immutable Raw identities."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
from datetime import UTC, datetime

from personal_data_platform.raw.models import RawObject

RAW_PREFIX = "raw/owntracks/v1/"
RAW_FIELDS = frozenset({"tst", "lat", "lon", "acc", "vel", "motionactivities"})
_KEY = re.compile(
    r"raw/owntracks/v1/(?P<device>[0-9a-f]{64})/location/"
    r"(?P<date>\d{4}/\d{2}/\d{2})/(?P<timestamp>\d{8}T\d{6}Z)_"
    r"(?P<sha>[0-9a-f]{64})\.json\.gz"
)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON field")
        result[name] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def location_message(payload: bytes, *, from_raw: bool = False) -> dict[str, object] | None:
    if not payload:
        return None
    try:
        message = json.loads(payload, object_pairs_hook=_unique, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid OwnTracks JSON") from None
    if not isinstance(message, dict):
        raise ValueError("OwnTracks message requires _type")
    kind = message.get("_type", "location" if from_raw else None)
    if not isinstance(kind, str):
        raise ValueError("OwnTracks message requires _type")
    if kind == "encrypted":
        raise ValueError("OwnTracks payload encryption is unsupported; use HTTPS")
    if kind != "location":
        return None
    for name, lower, upper in (("lat", -90, 90), ("lon", -180, 180)):
        value = message.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not lower <= value <= upper
        ):
            raise ValueError(f"invalid OwnTracks {name}")
    timestamp = message.get("tst")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise ValueError("invalid OwnTracks timestamp")
    try:
        datetime.fromtimestamp(timestamp, UTC)
    except (OverflowError, OSError, ValueError):
        raise ValueError("invalid OwnTracks timestamp") from None
    for name in ("acc", "alt", "vel"):
        value = message.get(name)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"invalid OwnTracks {name}")
    for name in ("m",):
        value = message.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not -1 <= value <= 100
        ):
            raise ValueError(f"invalid OwnTracks {name}")
    trigger = message.get("t")
    if trigger is not None and not isinstance(trigger, str):
        raise ValueError("invalid OwnTracks trigger")
    activities = message.get("motionactivities")
    if activities is not None and (
        not isinstance(activities, list)
        or any(not isinstance(activity, str) for activity in activities)
    ):
        raise ValueError("invalid OwnTracks motionactivities")
    return message


def prepare_location(payload: bytes, *, device_key: str) -> tuple[str, bytes, bytes] | None:
    if re.fullmatch(r"[0-9a-f]{64}", device_key) is None:
        raise ValueError("OwnTracks device_key must be a pseudonymous 64-character hex key")
    message = location_message(payload)
    if message is None:
        return None
    stored = {name: value for name, value in message.items() if name in RAW_FIELDS}
    canonical = json.dumps(
        stored, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    timestamp = message["tst"]
    assert isinstance(timestamp, int)
    observed = datetime.fromtimestamp(timestamp, UTC)
    key = (
        f"{RAW_PREFIX}{device_key}/location/{observed:%Y/%m/%d}/"
        f"{observed:%Y%m%dT%H%M%SZ}_{digest}.json.gz"
    )
    return key, canonical, gzip.compress(canonical, mtime=0)


def parse_raw_key(key: str, *, storage_created_at: datetime, storage_generation: int) -> RawObject:
    match = _KEY.fullmatch(key)
    if match is None:
        raise ValueError("invalid OwnTracks Raw key")
    observed = datetime.strptime(match["timestamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if observed.strftime("%Y/%m/%d") != match["date"]:
        raise ValueError("OwnTracks Raw date does not match timestamp")
    device, digest = match["device"], match["sha"]
    return RawObject(
        key=key,
        source_id="owntracks",
        schema_version=1,
        subject_key=device,
        stream="location",
        logical_key=hashlib.sha256(f"{device}:{digest}".encode()).hexdigest(),
        observed_at=observed,
        sha256=digest,
        storage_created_at=storage_created_at,
        storage_generation=storage_generation,
    )
