"""Complete Google Health v4 acquisitions and lossless source-point retention."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from personal_data_platform.sources.fitbit.models import (
    DATE_TYPES,
    Record,
    Snapshot,
    Window,
    date_cursor,
    object_dict,
    parse_time,
    string,
)

DATA_SOURCE_FAMILY = "users/me/dataSourceFamilies/google-wearables"
_FIELDS = {
    "steps": ("steps", "steps.interval.start_time"),
    "heart-rate": ("heartRate", "heart_rate.sample_time.physical_time"),
    "daily-resting-heart-rate": ("dailyRestingHeartRate", "daily_resting_heart_rate.date"),
    "active-zone-minutes": ("activeZoneMinutes", "active_zone_minutes.interval.start_time"),
    "sleep": ("sleep", "sleep.interval.civil_end_time"),
}


class HealthError(RuntimeError):
    """A permanent Google Health request failure, without response contents."""


class AuthenticationError(HealthError):
    """Authorization is expired, revoked, or missing a required permission."""


class TransientError(HealthError):
    """Transport or server failure; the caller can retry the whole acquisition."""


class RateLimitError(TransientError):
    """The provider rate limit was reached."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Google Health rate limit reached")
        self.retry_after = retry_after


class InvalidResponseError(HealthError):
    """The response cannot establish a complete, unambiguous replacement range."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Bounded HTTP transport; provider response text never enters error messages."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    response.status, response.read(), dict(response.headers.items())
                )
        except HTTPError as error:
            try:
                return HttpResponse(error.code, error.read(), dict(error.headers.items()))
            finally:
                error.close()
        except (URLError, OSError):
            raise TransientError("Google Health transport failed") from None


def request_json(
    transport: HttpTransport,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes | None = None,
    timeout: float,
    oauth: bool = False,
) -> dict[str, object]:
    """Validate and classify an API or refresh response without exposing its body."""
    try:
        response = transport.request(method, url, headers=headers, body=body, timeout=timeout)
    except (URLError, OSError):
        raise TransientError("Google Health transport failed") from None
    if response.status in (401, 403) or (oauth and response.status == 400):
        raise AuthenticationError("Google Health authorization failed")
    if response.status == 429:
        retry_after = next(
            (value for key, value in response.headers.items() if key.lower() == "retry-after"),
            "",
        )
        try:
            candidate = float(retry_after)
            delay: float | None = candidate if math.isfinite(candidate) and candidate >= 0 else None
        except ValueError:
            delay = None
        raise RateLimitError(delay)
    if response.status >= 500 or response.status in (408, 425):
        raise TransientError(f"Google Health server failed ({response.status})")
    if not 200 <= response.status < 300:
        raise HealthError(f"Google Health request failed ({response.status})")
    try:
        payload: object = json.loads(response.body)
        return object_dict(payload)
    except (ValueError, UnicodeDecodeError):
        raise InvalidResponseError("Google Health response must be a JSON object") from None


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _chunks(window: Window) -> Iterator[Window]:
    # Reconcile is paginated. These conservative client bounds also stay within
    # the provider's documented 14/90-day limits for aggregation endpoints.
    maximum = timedelta(days=14 if window.data_type == "heart-rate" else 90)
    start = window.start
    while start < window.end:
        end = min(start + maximum, window.end)
        yield Window(window.data_type, start, end)
        start = end


class HealthClient:
    """Return a snapshot only after every chunk and page has been validated.

    ``window`` is the actual cursor range to replace. The caller expands interval
    boundaries against existing records before invoking this method. Retries are
    owned by the durable task worker, so a failed page never produces a snapshot.
    """

    def __init__(
        self,
        *,
        access_token: Callable[[], str],
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_pages: int = 1000,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("HTTP timeout must be positive and finite")
        if max_pages <= 0:
            raise ValueError("page limit must be positive")
        self._access_token = access_token
        self._transport = transport if transport is not None else UrllibTransport()
        self._timeout = timeout
        self._clock = clock
        self._max_pages = max_pages

    def fetch(self, window: Window, *, subject_key: str) -> Snapshot:
        snapshot = Snapshot(subject_key, window, self._clock(), ())
        points: list[dict[str, object]] = []
        records: list[Record] = []
        identities: set[tuple[str, str]] = set()
        page_count = 0
        deadline = time.monotonic() + 1200
        for chunk in _chunks(window):
            page_token = ""
            seen_tokens: set[str] = set()
            while True:
                if page_count >= self._max_pages or time.monotonic() >= deadline:
                    raise InvalidResponseError("Google Health acquisition limit exceeded")
                page_count += 1
                page = self._page(chunk, page_token)
                if set(page) - {"dataPoints", "nextPageToken"}:
                    raise InvalidResponseError("Unexpected Google Health page fields")
                raw_points = page.get("dataPoints", [])
                if not isinstance(raw_points, list):
                    raise InvalidResponseError("Google Health dataPoints must be an array")
                for raw_point in raw_points:
                    try:
                        point = object_dict(raw_point)
                        normalized = _normalize(window.data_type, point, subject_key)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        raise InvalidResponseError("Malformed Google Health data point") from None
                    for record in normalized:
                        if not chunk.start <= record.cursor < chunk.end:
                            raise InvalidResponseError("Google Health point is outside query range")
                        identity = (record.kind, record.record_id)
                        if identity in identities:
                            raise InvalidResponseError(
                                "Google Health returned a duplicate identity"
                            )
                        identities.add(identity)
                    records.extend(normalized)
                    points.append(point)
                    if len(records) > 250_000:
                        raise InvalidResponseError("Google Health record limit exceeded")
                next_token = page.get("nextPageToken", "")
                if not isinstance(next_token, str):
                    raise InvalidResponseError("Google Health page token must be a string")
                if not next_token:
                    break
                if next_token in seen_tokens:
                    raise InvalidResponseError("Google Health repeated a page token")
                seen_tokens.add(next_token)
                page_token = next_token
        try:
            return replace(snapshot, records=tuple(records), source_payload=tuple(points))
        except ValueError:
            raise InvalidResponseError(
                "Google Health records do not form a valid snapshot"
            ) from None

    def _page(self, window: Window, page_token: str) -> dict[str, object]:
        _, field = _FIELDS[window.data_type]
        if window.data_type in DATE_TYPES:
            start, end = window.start.date().isoformat(), window.end.date().isoformat()
        else:
            start, end = _utc_text(window.start), _utc_text(window.end)
        query: dict[str, str | int] = {
            "filter": f'{field} >= "{start}" AND {field} < "{end}"',
            "pageSize": 25 if window.data_type == "sleep" else 10_000,
            "dataSourceFamily": DATA_SOURCE_FAMILY,
        }
        if page_token:
            query["pageToken"] = page_token
        token = self._access_token()
        if not isinstance(token, str) or not token.strip():
            raise AuthenticationError("Google Health access token is missing")
        return request_json(
            self._transport,
            "GET",
            f"https://health.googleapis.com/v4/users/me/dataTypes/{window.data_type}"
            f"/dataPoints:reconcile?{urlencode(query)}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=self._timeout,
        )


def _integer(value: object, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("expected an integer")
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value) is None:
        raise ValueError("expected an integer")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError("integer outside permitted range")
    return result


def _timestamp(value: object) -> datetime:
    text = string(value)
    fraction = re.search(r"\.(\d+)", text)
    if fraction is not None and any(digit != "0" for digit in fraction[1][6:]):
        raise ValueError("timestamp precision exceeds storage precision")
    return parse_time(text)


def _offset(value: object) -> int:
    text = string(value)
    if re.fullmatch(r"-?\d+(?:\.0+)?s", text) is None:
        raise ValueError("UTC offset must use whole seconds")
    return _integer(text[:-1].split(".")[0], minimum=-64800, maximum=64800)


def _date(value: object) -> date:
    fields = object_dict(value)
    return date(
        _integer(fields["year"], minimum=1, maximum=9999),
        _integer(fields["month"], minimum=1, maximum=12),
        _integer(fields["day"], minimum=1, maximum=31),
    )


def _source_date(value: object, instant: datetime, offset: int) -> date:
    local_date = (instant + timedelta(seconds=offset)).date()
    if value is None:
        return local_date
    provided = _date(object_dict(value)["date"])
    if provided != local_date:
        raise ValueError("provider civil date disagrees with time and UTC offset")
    return provided


def _identity(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _alias(fields: dict[str, object], primary: str, alternate: str) -> object:
    # The REST schema and guide examples use different names. Accept either,
    # but never silently choose between contradictory provider identities.
    if (
        primary in fields
        and alternate in fields
        and (
            type(fields[primary]) is not type(fields[alternate])
            or fields[primary] != fields[alternate]
        )
    ):
        raise ValueError("conflicting API field aliases")
    return fields[primary] if primary in fields else fields.get(alternate)


def _point_id(point: dict[str, object], data_type: str) -> str | None:
    name = _alias(point, "dataPointName", "name")
    if name in (None, ""):
        return None
    matched = re.fullmatch(
        rf"users/[^/]+/dataTypes/{re.escape(data_type)}/dataPoints/([^/]+)", string(name)
    )
    if matched is None:
        raise ValueError("invalid data point identity")
    return matched[1]


def _interval(fields: dict[str, object]) -> tuple[datetime, datetime, int, int]:
    start, end = _timestamp(fields["startTime"]), _timestamp(fields["endTime"])
    if start >= end:
        raise ValueError("interval must be positive")
    return start, end, _offset(fields["startUtcOffset"]), _offset(fields["endUtcOffset"])


def _normalize(data_type: str, point: dict[str, object], subject_key: str) -> tuple[Record, ...]:
    field, _ = _FIELDS[data_type]
    if any(other != field and other in point for other, _ in _FIELDS.values()):
        raise ValueError("data point contains multiple union fields")
    payload = object_dict(point[field])
    point_id = _point_id(point, data_type)
    if data_type == "sleep":
        if point_id is None:
            raise ValueError("sleep needs a provider identity")
        return _sleep(payload, point_id)
    if data_type == "daily-resting-heart-rate":
        source_date = _date(payload["date"])
        cursor = date_cursor(source_date)
        return (
            Record(
                kind=data_type,
                record_id=point_id or _identity(subject_key, data_type, source_date.isoformat()),
                cursor=cursor,
                start=cursor,
                source_date=source_date,
                value=float(_integer(payload["beatsPerMinute"], minimum=1, maximum=300)),
            ),
        )
    if data_type == "heart-rate":
        sample = object_dict(payload["sampleTime"])
        start = _timestamp(sample["physicalTime"])
        offset = _offset(sample["utcOffset"])
        return (
            Record(
                kind=data_type,
                record_id=point_id or _identity(subject_key, data_type, _utc_text(start)),
                cursor=start,
                start=start,
                offset_seconds=offset,
                source_date=_source_date(sample.get("civilTime"), start, offset),
                value=float(_integer(payload["beatsPerMinute"], minimum=1, maximum=300)),
            ),
        )
    timing = object_dict(payload["interval"])
    start, end, start_offset, end_offset = _interval(timing)
    value_field = "count" if data_type == "steps" else "activeZoneMinutes"
    value = _integer(payload[value_field], maximum=1_000_000 if data_type == "steps" else 2**63 - 1)
    category = None
    if data_type == "active-zone-minutes":
        category = string(payload["heartRateZone"]).lower()
        if category not in ("fat_burn", "cardio", "peak", "heart_rate_zone_unspecified"):
            raise ValueError("unrecognized heart rate zone")
    return (
        Record(
            kind=data_type,
            record_id=point_id
            or _identity(subject_key, data_type, _utc_text(start), _utc_text(end)),
            cursor=start,
            start=start,
            end=end,
            value=float(value),
            offset_seconds=start_offset,
            end_offset_seconds=end_offset,
            source_date=_source_date(timing.get("civilStartTime"), start, start_offset),
            category=category,
        ),
    )


def _sleep(payload: dict[str, object], point_id: str) -> tuple[Record, ...]:
    timing = object_dict(payload["interval"])
    start, end, start_offset, end_offset = _interval(timing)
    source_date = _source_date(timing.get("civilEndTime"), end, end_offset)
    cursor = date_cursor(source_date)
    summary = object_dict(payload.get("summary", {}))
    minutes = summary.get("minutesAsleep")
    main_sleep = _alias(object_dict(payload.get("metadata", {})), "mainSleep", "main")
    if main_sleep is not None and not isinstance(main_sleep, bool):
        raise ValueError("sleep classification must be boolean")
    category = string(payload["type"]).lower() if "type" in payload else None
    records = [
        Record(
            kind="sleep",
            record_id=point_id,
            cursor=cursor,
            start=start,
            end=end,
            offset_seconds=start_offset,
            end_offset_seconds=end_offset,
            source_date=source_date,
            value=float(_integer(minutes)) if minutes is not None else None,
            is_main_sleep=main_sleep,
            category=category,
        )
    ]
    stage_intervals: list[tuple[datetime, datetime]] = []
    for field, kind, wake_category in (
        ("stages", "sleep-stage", None),
        ("shortAwakenings", "sleep-wake", "short-awakening"),
        ("outOfBedSegments", "sleep-wake", "out-of-bed"),
    ):
        children = payload.get(field, [])
        if not isinstance(children, list):
            raise ValueError("sleep details must be an array")
        for child in children:
            detail = object_dict(child)
            child_start, child_end = _timestamp(detail["startTime"]), _timestamp(detail["endTime"])
            if not start <= child_start < child_end <= end:
                raise ValueError("sleep detail outside session")
            detail_category = wake_category or string(detail["type"]).lower()
            if kind == "sleep-stage":
                if detail_category not in (
                    "awake",
                    "light",
                    "deep",
                    "rem",
                    "asleep",
                    "restless",
                    "sleep_stage_type_unspecified",
                ):
                    raise ValueError("unrecognized sleep stage")
                stage_intervals.append((child_start, child_end))
            # The guide's shortAwakenings objects omit local offsets. Keep them
            # unknown rather than inferring a timezone across an offset change.
            child_start_offset = (
                _offset(detail["startUtcOffset"]) if "startUtcOffset" in detail else None
            )
            child_end_offset = _offset(detail["endUtcOffset"]) if "endUtcOffset" in detail else None
            if field != "shortAwakenings" and (
                child_start_offset is None or child_end_offset is None
            ):
                raise ValueError("sleep detail needs its UTC offsets")
            records.append(
                Record(
                    kind=kind,
                    record_id=_identity(
                        point_id,
                        kind,
                        wake_category or "",
                        _utc_text(child_start),
                        _utc_text(child_end),
                    ),
                    cursor=cursor,
                    start=child_start,
                    end=child_end,
                    offset_seconds=child_start_offset,
                    end_offset_seconds=child_end_offset,
                    source_date=source_date,
                    parent_id=point_id,
                    category=detail_category,
                    value=(child_end - child_start).total_seconds(),
                )
            )
    ordered_stages = sorted(stage_intervals)
    for previous, following in zip(ordered_stages, ordered_stages[1:]):
        if previous[1] > following[0]:
            raise ValueError("sleep stages must not overlap")
    return tuple(records)
