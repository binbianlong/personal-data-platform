"""Validated, versioned records and the exact scope of a complete acquisition."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time

DATA_TYPES = ("steps", "heart-rate", "daily-resting-heart-rate", "active-zone-minutes", "sleep")
DATE_TYPES = ("daily-resting-heart-rate", "sleep")
TABLES = {
    "steps": "fitbit_steps",
    "heart-rate": "fitbit_heart_rate",
    "daily-resting-heart-rate": "fitbit_resting_heart_rate",
    "active-zone-minutes": "fitbit_active_zone",
    "sleep": "fitbit_sleep",
    "sleep-stage": "fitbit_sleep_stage",
    "sleep-wake": "fitbit_sleep_wake",
}
PARSER_VERSION = "fitbit-v1"


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

    def __post_init__(self) -> None:
        if self.data_type not in DATA_TYPES:
            raise ValueError("unsupported Fitbit data type")
        object.__setattr__(self, "start", aware(self.start))
        object.__setattr__(self, "end", aware(self.end))
        if self.start >= self.end:
            raise ValueError("window must have a positive range")
        if self.data_type in DATE_TYPES and any(
            value != date_cursor(value.date()) for value in (self.start, self.end)
        ):
            raise ValueError("civil date bounds must use UTC midnight")


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
        if self.kind in DATA_TYPES[:4] and self.value is None:
            raise ValueError("metric record needs a value")
        for offset in (self.offset_seconds, self.end_offset_seconds):
            if offset is not None and (
                isinstance(offset, bool) or not isinstance(offset, int) or abs(offset) > 64800
            ):
                raise ValueError("invalid UTC offset")
        if self.kind in ("sleep-stage", "sleep-wake") and not self.parent_id:
            raise ValueError("sleep detail needs a parent")
        if self.kind in ("steps", "heart-rate", "active-zone-minutes"):
            if self.cursor != self.start:
                raise ValueError("record cursor must match its physical start")
        if self.kind in DATE_TYPES:
            if self.source_date is None or self.cursor != date_cursor(self.source_date):
                raise ValueError("record cursor must match its provider date")


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("unsupported JSON value")


def object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("expected a JSON object")
    return dict(value)


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected an integer")
    return value


def record_from_dict(value: object) -> Record:
    data = object_dict(value)
    number = data.get("value")
    if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))):
        raise ValueError("expected a numeric record value")
    main = data.get("is_main_sleep")
    if main is not None and not isinstance(main, bool):
        raise ValueError("expected a boolean sleep classification")
    return Record(
        kind=string(data["kind"]),
        record_id=string(data["record_id"]),
        cursor=parse_time(string(data["cursor"])),
        start=parse_time(string(data["start"])),
        end=parse_time(string(data["end"])) if data.get("end") is not None else None,
        value=float(number) if number is not None else None,
        offset_seconds=_optional_int(data.get("offset_seconds")),
        end_offset_seconds=_optional_int(data.get("end_offset_seconds")),
        source_date=date.fromisoformat(string(data["source_date"]))
        if data.get("source_date") is not None
        else None,
        parent_id=string(data["parent_id"]) if data.get("parent_id") is not None else None,
        category=string(data["category"]) if data.get("category") is not None else None,
        is_main_sleep=main,
    )


@dataclass(frozen=True, slots=True)
class Snapshot:
    subject_key: str
    window: Window
    fetched_at: datetime
    records: tuple[Record, ...]
    origin: str = "api"
    complete: bool = True
    source_payload: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
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
            if not self.window.start <= record.cursor < self.window.end:
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

    def to_bytes(self) -> bytes:
        return json.dumps(
            {"schema_version": 1, **asdict(self)},
            default=_json_default,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

    def source_sha256(self) -> str:
        """Hash every persisted source field except observation time."""
        payload = json.loads(self.to_bytes())
        del payload["fetched_at"]
        # API pages do not promise a stable order. Raw keeps their original
        # sequence, while equality compares the same complete set of points.
        for field in ("records", "source_payload"):
            payload[field].sort(
                key=lambda item: json.dumps(
                    item, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
            )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    @classmethod
    def from_bytes(cls, payload: bytes) -> Snapshot:
        data = object_dict(json.loads(payload))
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported Fitbit Raw schema")
        window = object_dict(data["window"])
        rows = data["records"]
        source_payload = data.get("source_payload", [])
        if not isinstance(rows, list) or not isinstance(source_payload, list):
            raise ValueError("records and source_payload must be arrays")
        if data.get("complete") is not True:
            raise ValueError("Raw acquisition is not complete")
        return cls(
            subject_key=string(data["subject_key"]),
            window=Window(
                string(window["data_type"]),
                parse_time(string(window["start"])),
                parse_time(string(window["end"])),
            ),
            fetched_at=parse_time(string(data["fetched_at"])),
            records=tuple(record_from_dict(row) for row in rows),
            origin=string(data["origin"]),
            source_payload=tuple(object_dict(point) for point in source_payload),
        )


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

    def __post_init__(self) -> None:
        if not self.notification_id or len(self.notification_id) > 256 or not self.windows:
            raise ValueError("notification needs an identity and acquisition windows")
        AcquisitionScope(self.subject_key, self.windows[0], "fitbit-v2")
        if len(set(self.windows)) != len(self.windows):
            raise ValueError("duplicate notification windows")
        object.__setattr__(self, "received_at", aware(self.received_at))


HEART_RATE_AGGREGATION_VERSION = "heart-rate-minute-v1"
GOOGLE_WEARABLES = "users/me/dataSourceFamilies/google-wearables"


def _canonical_pages(pages: tuple[dict[str, object], ...], field: str) -> dict[str, object]:
    """Compare source data independently of page boundaries and transport tokens."""
    points: list[object] = []
    metadata: set[str] = set()
    for page in pages:
        rows = page.get(field, [])
        if not isinstance(rows, list):
            raise ValueError("source page records must be an array")
        points.extend(rows)
        rest = {key: value for key, value in page.items() if key not in (field, "nextPageToken")}
        if rest:
            metadata.add(json.dumps(rest, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return {
        "points": sorted(
            points,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), allow_nan=False
            ),
        ),
        "page_metadata": [json.loads(item) for item in sorted(metadata)],
    }


def _source_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, default=_json_default, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CapturedSnapshot:
    snapshot: Snapshot
    pages: tuple[dict[str, object], ...]

    @property
    def aggregation_version(self) -> str:
        return "fitbit-v2"

    @property
    def subject_key(self) -> str:
        return self.snapshot.subject_key

    @property
    def window(self) -> Window:
        return self.snapshot.window

    @property
    def fetched_at(self) -> datetime:
        return self.snapshot.fetched_at

    @property
    def origin(self) -> str:
        return self.snapshot.origin

    def source_sha256(self) -> str:
        return _source_digest(
            {
                "snapshot": self.snapshot.source_sha256(),
                "aggregation_version": self.aggregation_version,
                "source": _canonical_pages(self.pages, "dataPoints"),
            }
        )

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": 2,
                "snapshot": json.loads(self.snapshot.to_bytes()),
                "pages": self.pages,
            },
            default=_json_default,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

    @classmethod
    def from_bytes(cls, payload: bytes) -> CapturedSnapshot:
        data = object_dict(json.loads(payload))
        if type(data.get("schema_version")) is not int or data["schema_version"] != 2:
            raise ValueError("unsupported captured Fitbit schema")
        pages = data["pages"]
        if not isinstance(pages, list):
            raise ValueError("pages must be an array")
        return cls(
            Snapshot.from_bytes(json.dumps(data["snapshot"]).encode()),
            tuple(object_dict(page) for page in pages),
        )


@dataclass(frozen=True, slots=True)
class HeartRateMinute:
    start: datetime
    end: datetime
    average: float
    minimum: float
    maximum: float
    data_source_family: str = GOOGLE_WEARABLES
    sample_count: int | None = None
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
        if (
            self.data_source_family != GOOGLE_WEARABLES
            or self.origin != "api"
            or self.aggregation_version != HEART_RATE_AGGREGATION_VERSION
        ):
            raise ValueError("unsupported heart rate aggregation source")
        if self.sample_count is not None and (
            type(self.sample_count) is not int or self.sample_count <= 0
        ):
            raise ValueError("sample count must be positive or unknown")


def minute_from_dict(value: object) -> HeartRateMinute:
    row = object_dict(value)

    def number(name: str) -> float:
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("heart rate statistic must be numeric")
        return float(value)

    return HeartRateMinute(
        parse_time(string(row["start"])),
        parse_time(string(row["end"])),
        number("average"),
        number("minimum"),
        number("maximum"),
        string(row["data_source_family"]),
        _optional_int(row.get("sample_count")),
        string(row["origin"]),
        string(row["aggregation_version"]),
    )


@dataclass(frozen=True, slots=True)
class HeartRateMinuteSnapshot:
    subject_key: str
    window: Window
    fetched_at: datetime
    minutes: tuple[HeartRateMinute, ...]
    pages: tuple[dict[str, object], ...]
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

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": 2,
                "subject_key": self.subject_key,
                "window": asdict(self.window),
                "fetched_at": self.fetched_at,
                "minutes": [asdict(row) for row in self.minutes],
                "pages": self.pages,
                "origin": self.origin,
                "aggregation_version": self.aggregation_version,
                "complete": self.complete,
            },
            default=_json_default,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

    def source_sha256(self) -> str:
        payload = object_dict(json.loads(self.to_bytes()))
        del payload["fetched_at"]
        del payload["pages"]
        payload["minutes"] = sorted(
            [asdict(row) for row in self.minutes], key=lambda row: row["start"]
        )
        payload["source"] = _canonical_pages(self.pages, "rollupDataPoints")
        return _source_digest(payload)

    @classmethod
    def from_bytes(cls, payload: bytes) -> HeartRateMinuteSnapshot:
        data = object_dict(json.loads(payload))
        if (
            type(data.get("schema_version")) is not int
            or data["schema_version"] != 2
            or data.get("complete") is not True
        ):
            raise ValueError("unsupported or incomplete minute Fitbit schema")
        window = object_dict(data["window"])
        minutes, pages = data["minutes"], data["pages"]
        if not isinstance(minutes, list) or not isinstance(pages, list):
            raise ValueError("minutes and pages must be arrays")
        return cls(
            string(data["subject_key"]),
            Window(
                string(window["data_type"]),
                parse_time(string(window["start"])),
                parse_time(string(window["end"])),
            ),
            parse_time(string(data["fetched_at"])),
            tuple(minute_from_dict(row) for row in minutes),
            tuple(object_dict(page) for page in pages),
            string(data["origin"]),
            string(data["aggregation_version"]),
        )


@dataclass(frozen=True, slots=True)
class BundleEntry:
    attempt_id: str
    acquisition: CapturedSnapshot | HeartRateMinuteSnapshot
    requested_scope: AcquisitionScope | None = None

    def __post_init__(self) -> None:
        if self.requested_scope is None:
            object.__setattr__(
                self,
                "requested_scope",
                AcquisitionScope(
                    self.acquisition.subject_key,
                    self.acquisition.window,
                    self.acquisition.aggregation_version,
                ),
            )
        if not self.attempt_id:
            raise ValueError("bundle entry requires an attempt")
        if self.requested_scope is not None and (
            self.requested_scope.subject_key != self.acquisition.subject_key
            or self.requested_scope.window.data_type != self.acquisition.window.data_type
            or self.requested_scope.aggregation_version != self.acquisition.aggregation_version
            or not self.requested_scope.window.start
            <= self.acquisition.window.start
            < self.acquisition.window.end
            <= self.requested_scope.window.end
        ):
            raise ValueError("bundle acquisition does not match requested scope")

    @property
    def scope(self) -> AcquisitionScope:
        return self.requested_scope or AcquisitionScope(
            self.acquisition.subject_key,
            self.acquisition.window,
            self.acquisition.aggregation_version,
        )


@dataclass(frozen=True, slots=True)
class FitbitBundle:
    bundle_id: str
    entries: tuple[BundleEntry, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.bundle_id) is None or not self.entries:
            raise ValueError("invalid bundle identity or empty entries")
        if len({entry.attempt_id for entry in self.entries}) != len(self.entries):
            raise ValueError("bundle has duplicate attempts")
        if len({entry.acquisition.subject_key for entry in self.entries}) != 1:
            raise ValueError("bundle must have one subject")
