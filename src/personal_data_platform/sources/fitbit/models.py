"""Validated, versioned records and the exact scope of a complete acquisition."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

DATA_TYPES = ("steps", "heart-rate", "daily-resting-heart-rate", "active-zone-minutes", "sleep")
DATE_TYPES = ("daily-resting-heart-rate", "sleep")
RECORD_AGGREGATION_VERSION = "records-v1"
CIVIL_RECORD_AGGREGATION_VERSION = "records-civil-v1"
AcquisitionMode = Literal["legacy", "bounded_minutes", "bounded_steps"]
TimeBasis = Literal["utc", "civil"]
TABLES = {
    "steps": "fitbit_activity_interval",
    "daily-resting-heart-rate": "fitbit_resting_heart_rate_daily",
    "active-zone-minutes": "fitbit_activity_interval",
    "sleep": "fitbit_sleep_session",
    "sleep-stage": "fitbit_sleep_detail",
    "sleep-wake": "fitbit_sleep_detail",
}


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)


def parse_time(value: str) -> datetime:
    return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def date_cursor(value: date) -> datetime:
    """Represent a provider civil date without interpreting it as a physical day."""
    return datetime.combine(value, time(), UTC)


@dataclass(frozen=True, slots=True)
class Window:
    data_type: str
    start: datetime
    end: datetime
    time_basis: TimeBasis = "utc"

    def __post_init__(self) -> None:
        if self.data_type not in DATA_TYPES:
            raise ValueError("unsupported Fitbit data type")
        object.__setattr__(self, "start", aware(self.start))
        object.__setattr__(self, "end", aware(self.end))
        if self.start >= self.end:
            raise ValueError("window must have a positive range")
        if self.time_basis not in ("utc", "civil"):
            raise ValueError("unsupported time basis")
        if self.time_basis == "civil" and (
            self.data_type != "steps"
            or any(value != date_cursor(value.date()) for value in (self.start, self.end))
        ):
            raise ValueError("civil steps require provider date bounds")
        if self.data_type in DATE_TYPES and any(
            value != date_cursor(value.date()) for value in (self.start, self.end)
        ):
            raise ValueError("civil date bounds must use UTC midnight")

    def record_cursor(self, record: Record) -> datetime:
        if self.time_basis == "civil":
            if record.source_date is None or record.offset_seconds is None:
                raise ValueError("civil steps require an unambiguous provider date")
            local_date = (record.start + timedelta(seconds=record.offset_seconds)).date()
            if local_date != record.source_date:
                raise ValueError("provider date disagrees with UTC offset")
            return date_cursor(record.source_date)
        return record.cursor


@dataclass(frozen=True, slots=True)
class Record:
    kind: str
    record_id: str
    cursor: datetime
    start: datetime
    end: datetime | None = None
    value: float | None = None
    offset_seconds: int | None = None
    end_offset_seconds: int | None = None
    source_date: date | None = None
    parent_id: str | None = None
    category: str | None = None
    is_main_sleep: bool | None = None

    def __post_init__(self) -> None:
        if self.kind not in TABLES or not self.record_id:
            raise ValueError("record must have a supported kind and identity")
        object.__setattr__(self, "cursor", aware(self.cursor))
        object.__setattr__(self, "start", aware(self.start))
        if self.end is not None:
            object.__setattr__(self, "end", aware(self.end))
            if self.end <= self.start:
                raise ValueError("record interval must be positive")
        if self.kind in ("steps", "active-zone-minutes", "sleep", "sleep-stage", "sleep-wake"):
            if self.end is None:
                raise ValueError("interval record needs an end")
        if self.value is not None and (
            isinstance(self.value, bool) or not math.isfinite(self.value) or self.value < 0
        ):
            raise ValueError("record value must be finite and nonnegative")
        if self.value is not None:
            object.__setattr__(self, "value", float(self.value))
        if self.kind in DATA_TYPES[:4] and self.value is None:
            raise ValueError("metric record needs a value")
        for offset in (self.offset_seconds, self.end_offset_seconds):
            if offset is not None and (
                isinstance(offset, bool) or not isinstance(offset, int) or abs(offset) > 64800
            ):
                raise ValueError("invalid UTC offset")
        if self.kind in ("sleep-stage", "sleep-wake") and not self.parent_id:
            raise ValueError("sleep detail needs a parent")
        if self.kind in ("steps", "active-zone-minutes"):
            if self.cursor != self.start:
                raise ValueError("record cursor must match its physical start")
        if self.kind in DATE_TYPES:
            if self.source_date is None or self.cursor != date_cursor(self.source_date):
                raise ValueError("record cursor must match its provider date")


def object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("expected a JSON object")
    return dict(value)


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


@dataclass(frozen=True, slots=True)
class Snapshot:
    subject_key: str
    window: Window
    fetched_at: datetime
    records: tuple[Record, ...]
    origin: str = "api"
    complete: bool = True

    def __post_init__(self) -> None:
        if self.window.data_type == "heart-rate":
            raise ValueError("heart rate requires a minute snapshot")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.subject_key) is None:
            raise ValueError("invalid pseudonymous subject key")
        if self.origin != "api":
            raise ValueError("unsupported acquisition origin")
        if self.complete is not True:
            raise ValueError("only complete acquisitions can replace a range")
        object.__setattr__(self, "fetched_at", aware(self.fetched_at))
        identities: set[tuple[str, str]] = set()
        sessions = {record.record_id: record for record in self.records if record.kind == "sleep"}
        for record in self.records:
            kind = "sleep" if record.kind in ("sleep-stage", "sleep-wake") else record.kind
            if kind != self.window.data_type:
                raise ValueError("record belongs to another data type")
            if not self.window.start <= self.window.record_cursor(record) < self.window.end:
                raise ValueError("record is outside the acquired range")
            identity = (record.kind, record.record_id)
            if identity in identities:
                raise ValueError("duplicate record identity")
            identities.add(identity)
            if record.kind in ("sleep-stage", "sleep-wake"):
                parent = sessions.get(record.parent_id or "")
                if parent is None or parent.cursor != record.cursor:
                    raise ValueError("sleep detail does not match a session")
                if (
                    parent.end is None
                    or record.end is None
                    or not (parent.start <= record.start < record.end <= parent.end)
                ):
                    raise ValueError("sleep detail is outside its session")

    @property
    def aggregation_version(self) -> str:
        return RECORD_AGGREGATION_VERSION


@dataclass(frozen=True, slots=True)
class AcquisitionScope:
    subject_key: str
    window: Window
    aggregation_version: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.subject_key) is None:
            raise ValueError("invalid pseudonymous subject key")
        if not self.aggregation_version:
            raise ValueError("aggregation version is required")

    @property
    def key(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [
                    self.subject_key,
                    self.window.data_type,
                    self.window.start.isoformat(),
                    self.window.end.isoformat(),
                    self.aggregation_version,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class Notification:
    notification_id: str
    subject_key: str
    windows: tuple[Window, ...]
    received_at: datetime
    acquisition_mode: AcquisitionMode = "legacy"

    def __post_init__(self) -> None:
        if not self.notification_id or len(self.notification_id) > 256 or not self.windows:
            raise ValueError("notification needs an identity and acquisition windows")
        AcquisitionScope(self.subject_key, self.windows[0], RECORD_AGGREGATION_VERSION)
        if len(set(self.windows)) != len(self.windows):
            raise ValueError("duplicate notification windows")
        if self.acquisition_mode not in ("legacy", "bounded_minutes", "bounded_steps"):
            raise ValueError("unsupported notification acquisition mode")
        if self.acquisition_mode == "bounded_minutes" and any(
            window.data_type != "heart-rate"
            or any(bound.second or bound.microsecond for bound in (window.start, window.end))
            for window in self.windows
        ):
            raise ValueError("bounded minutes require aligned heart-rate windows")
        if self.acquisition_mode == "bounded_steps" and any(
            window.data_type != "steps" for window in self.windows
        ):
            raise ValueError("bounded steps require steps windows")
        if self.acquisition_mode != "bounded_steps" and any(
            window.time_basis != "utc" for window in self.windows
        ):
            raise ValueError("civil windows require bounded steps")
        object.__setattr__(self, "received_at", aware(self.received_at))


HEART_RATE_AGGREGATION_VERSION = "heart-rate-minute-v1"
GOOGLE_WEARABLES = "users/me/dataSourceFamilies/google-wearables"


@dataclass(frozen=True, slots=True)
class HeartRateMinute:
    start: datetime
    end: datetime
    average: float
    minimum: float
    maximum: float
    data_source_family: str = GOOGLE_WEARABLES
    origin: str = "api"
    aggregation_version: str = HEART_RATE_AGGREGATION_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", aware(self.start))
        object.__setattr__(self, "end", aware(self.end))
        if (
            self.start.second
            or self.start.microsecond
            or (self.end - self.start).total_seconds() != 60
        ):
            raise ValueError("heart rate window must be one complete UTC minute")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in (self.average, self.minimum, self.maximum)
        ):
            raise ValueError("heart rate values must be finite and nonnegative")
        if not self.minimum <= self.average <= self.maximum:
            raise ValueError("heart rate statistics are inconsistent")
        for name in ("average", "minimum", "maximum"):
            object.__setattr__(self, name, float(getattr(self, name)))
        if (
            self.data_source_family != GOOGLE_WEARABLES
            or self.origin != "api"
            or self.aggregation_version != HEART_RATE_AGGREGATION_VERSION
        ):
            raise ValueError("unsupported heart rate aggregation source")


@dataclass(frozen=True, slots=True)
class HeartRateMinuteSnapshot:
    subject_key: str
    window: Window
    fetched_at: datetime
    minutes: tuple[HeartRateMinute, ...]
    origin: str = "api"
    aggregation_version: str = HEART_RATE_AGGREGATION_VERSION
    complete: bool = True

    def __post_init__(self) -> None:
        AcquisitionScope(self.subject_key, self.window, self.aggregation_version)
        object.__setattr__(self, "fetched_at", aware(self.fetched_at))
        if self.window.data_type != "heart-rate" or any(
            bound.second or bound.microsecond for bound in (self.window.start, self.window.end)
        ):
            raise ValueError("heart rate acquisition needs complete UTC minutes")
        if self.window.end > self.fetched_at:
            raise ValueError("heart rate acquisition includes incomplete future minutes")
        if (
            self.origin != "api"
            or self.aggregation_version != HEART_RATE_AGGREGATION_VERSION
            or self.complete is not True
        ):
            raise ValueError("unsupported or incomplete heart rate acquisition")
        seen: set[datetime] = set()
        for minute in self.minutes:
            if not self.window.start <= minute.start < minute.end <= self.window.end:
                raise ValueError("heart rate minute outside acquisition range")
            if minute.start in seen:
                raise ValueError("duplicate heart rate minute")
            seen.add(minute.start)


@dataclass(frozen=True, slots=True)
class WriteResult:
    outcome: Literal["range_replaced", "unchanged", "stale"]
    submitted_rows: int
    accepted_ranges: tuple[tuple[datetime, datetime], ...]
    protected_records: int = 0
