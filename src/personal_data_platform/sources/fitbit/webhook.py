"""Validate a complete Google Health delivery before producing safe work scopes."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import DATA_TYPES, DATE_TYPES, Window, date_cursor, object_dict, parse_time, string


class AuthenticationError(ValueError):
    """The sender, signature, or configured owner could not be verified."""


class PayloadError(ValueError):
    """The complete request cannot safely become replacement windows."""


class SignatureVerifier(Protocol):
    def verify(self, signature: bytes, payload: bytes) -> bool: ...


@dataclass(frozen=True, slots=True)
class Verification:
    """The authorized subscriber handshake carries no work."""


@dataclass(frozen=True, slots=True)
class VerifiedNotification:
    subject_key: str
    windows: tuple[Window, ...]
    groups: tuple[tuple[Window, ...], ...] = ()


def merge_windows(windows: tuple[Window, ...]) -> tuple[Window, ...]:
    """Merge overlapping/adjacent scopes only within the same metric."""
    merged: list[Window] = []
    for kind in DATA_TYPES:
        for window in sorted(
            (window for window in windows if window.data_type == kind),
            key=lambda window: window.start,
        ):
            if merged and merged[-1].data_type == kind and window.start <= merged[-1].end:
                previous = merged.pop()
                merged.append(Window(kind, previous.start, max(previous.end, window.end)))
            else:
                merged.append(window)
    return tuple(merged)


def _civil_datetime(value: object) -> datetime:
    if isinstance(value, str):
        result = datetime.fromisoformat(value)
        if "T" not in value or result.tzinfo is not None:
            raise ValueError("civil time must have no timezone")
        return result
    fields = object_dict(value)
    day = object_dict(fields["date"])
    clock = object_dict(fields.get("time", {}))

    def integer(mapping: dict[str, object], key: str, default: int = 0) -> int:
        number = mapping.get(key, default)
        if type(number) is not int:
            raise ValueError("civil time component must be an integer")
        return number

    nanos = integer(clock, "nanos")
    if not 0 <= nanos < 1_000_000_000:
        raise ValueError("invalid civil time nanoseconds")
    return datetime(
        integer(day, "year"),
        integer(day, "month"),
        integer(day, "day"),
        integer(clock, "hours"),
        integer(clock, "minutes"),
        integer(clock, "seconds"),
        nanos // 1000,
    )


def _civil_interval(interval: dict[str, object]) -> tuple[datetime, datetime] | None:
    bounds: tuple[datetime, datetime] | None = None
    for name, start_key, end_key in (
        ("civilIso8601TimeInterval", "startTime", "endTime"),
        ("civilDateTimeInterval", "startDateTime", "endDateTime"),
    ):
        if name not in interval:
            continue
        fields = object_dict(interval[name])
        candidate = (_civil_datetime(fields[start_key]), _civil_datetime(fields[end_key]))
        if candidate[0] >= candidate[1]:
            raise ValueError("civil interval must have a positive range")
        if bounds is not None and candidate != bounds:
            raise ValueError("civil interval representations disagree")
        bounds = candidate
    return bounds


def _window(kind: str, value: object) -> Window:
    interval = object_dict(value)
    civil = _civil_interval(interval)
    physical: tuple[datetime, datetime] | None = None
    if "physicalTimeInterval" in interval:
        fields = object_dict(interval["physicalTimeInterval"])
        physical = (parse_time(string(fields["startTime"])), parse_time(string(fields["endTime"])))
        if physical[0] >= physical[1]:
            raise ValueError("physical interval must have a positive range")
    if kind in DATE_TYPES:
        if civil is None:
            raise ValueError("date metric requires civil bounds")
        start, end = civil
        lower = date_cursor(start.date())
        upper = date_cursor(end.date())
        # Sleep uses the session's civil end date. Even a midnight end belongs
        # to that date; daily aggregates instead use their half-open day range.
        if kind == "sleep" or end.time() != datetime.min.time():
            upper += timedelta(days=1)
        return Window(kind, lower, upper)
    if physical is not None:
        return Window(kind, *physical)
    if civil is None:
        raise ValueError("notification has no interval")
    # A full-day notification intentionally omits physical bounds. Without a
    # provider offset, query a conservative UTC envelope covering every legal
    # offset accepted by the source model. The API still returns wearable data.
    return Window(
        kind,
        civil[0].replace(tzinfo=UTC) - timedelta(hours=18),
        civil[1].replace(tzinfo=UTC) + timedelta(hours=18),
    )


class GoogleHealthAuthenticator:
    def __init__(
        self,
        *,
        authorization: str,
        health_user_id: str,
        subject_key: str,
        signatures: SignatureVerifier,
    ) -> None:
        if not authorization or not health_user_id:
            raise ValueError("webhook authorization and owner must be configured")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", subject_key) is None:
            raise ValueError("invalid pseudonymous subject key")
        self._authorization = authorization.encode()
        self._owner = health_user_id.encode()
        self._subject = subject_key
        self._signatures = signatures

    def authenticate(
        self,
        *,
        authorization: str | None,
        signature_header: str | None,
        content_type: str | None,
        body: bytes,
    ) -> Verification | VerifiedNotification:
        if not hmac.compare_digest((authorization or "").encode(), self._authorization):
            raise AuthenticationError("webhook authorization failed")
        if (content_type or "").split(";", 1)[0].strip().lower() != "application/json":
            raise PayloadError("webhook requires application/json")
        try:
            payload: object = json.loads(body)
        except (UnicodeError, ValueError) as error:
            raise PayloadError("invalid webhook JSON") from error
        if payload == {"type": "verification"}:
            return Verification()
        encoded = (signature_header or "").strip()
        try:
            signature = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        except (ValueError, binascii.Error) as error:
            raise AuthenticationError("invalid webhook signature") from error
        if not signature or not self._signatures.verify(signature, body):
            raise AuthenticationError("webhook signature verification failed")
        notifications = payload if isinstance(payload, list) else [payload]
        if not 1 <= len(notifications) <= 99:
            raise PayloadError("webhook requires between one and 99 notifications")
        windows: list[Window] = []
        groups: list[tuple[Window, ...]] = []
        try:
            for notification in notifications:
                data = object_dict(object_dict(notification)["data"])
                if not hmac.compare_digest(string(data["healthUserId"]).encode(), self._owner):
                    raise AuthenticationError("unexpected webhook owner")
                kind = string(data["dataType"])
                if kind not in DATA_TYPES or data.get("version") != "1":
                    raise ValueError("unsupported webhook type or version")
                if data.get("operation") not in ("UPSERT", "DELETE"):
                    raise ValueError("unsupported webhook operation")
                intervals = data["intervals"]
                if not isinstance(intervals, list) or not intervals:
                    raise ValueError("webhook intervals must be nonempty")
                group = merge_windows(tuple(_window(kind, interval) for interval in intervals))
                groups.append(group)
                windows.extend(group)
        except AuthenticationError:
            raise
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            raise PayloadError("invalid notification batch") from error
        return VerifiedNotification(self._subject, merge_windows(tuple(windows)), tuple(groups))
