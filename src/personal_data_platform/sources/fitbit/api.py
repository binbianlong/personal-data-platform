"""Complete Google Health v4 acquisitions and lossless source-point retention."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import (
    DATE_TYPES,
    HeartRateMinute,
    HeartRateMinuteSnapshot,
    Record,
    Snapshot,
    Window,
    aware,
    data_source_family_for,
    object_dict,
)
from .normalization import (
    normalize,
    normalize_heart_rate_minute,
    parse_timestamp,
    utc_text,
)

DATA_SOURCE_FAMILY = "users/me/dataSourceFamilies/google-wearables"
LOGGER = logging.getLogger(__name__)
_FILTER_FIELDS = {
    "steps": "steps.interval.start_time",
    "daily-resting-heart-rate": "daily_resting_heart_rate.date",
    "active-zone-minutes": "active_zone_minutes.interval.start_time",
    "sleep": "sleep.interval.civil_end_time",
    "daily-heart-rate-variability": "daily_heart_rate_variability.date",
    "heart-rate-variability": "heart_rate_variability.sample_time.physical_time",
    "daily-respiratory-rate": "daily_respiratory_rate.date",
    "respiratory-rate-sleep-summary": "respiratory_rate_sleep_summary.sample_time.physical_time",
    "daily-oxygen-saturation": "daily_oxygen_saturation.date",
    "daily-sleep-temperature-derivations": "daily_sleep_temperature_derivations.date",
    "daily-vo2-max": "daily_vo2_max.date",
    "exercise": "exercise.interval.civil_start_time",
    "sedentary-period": "sedentary_period.interval.start_time",
    "distance": "distance.interval.start_time",
    "time-in-heart-rate-zone": "time_in_heart_rate_zone.interval.start_time",
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


def _log_request_failure(
    *, oauth: bool, http_status: int | None = None, error: OSError | None = None
) -> None:
    LOGGER.warning(
        "fitbit request failed",
        extra={
            "event": "fitbit_request_failed",
            "summary": {
                "request_kind": "oauth" if oauth else "api",
                "http_status": http_status,
                "transport_error_type": type(error).__name__ if error is not None else None,
                "transport_reason_type": type(error.reason).__name__
                if isinstance(error, URLError)
                else None,
            },
        },
    )


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
    except (URLError, OSError) as error:
        _log_request_failure(oauth=oauth, error=error)
        raise TransientError("Google Health transport failed") from None
    if not 200 <= response.status < 300:
        _log_request_failure(oauth=oauth, http_status=response.status)
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
        payload = object_dict(json.loads(response.body))
    except (ValueError, UnicodeDecodeError):
        raise InvalidResponseError("Google Health response must be a JSON object") from None
    if "error" in payload:
        raise InvalidResponseError("Google Health response contains an error")
    return payload


def _chunks(window: Window) -> Iterator[Window]:
    # Reconcile is paginated. These conservative client bounds also stay within
    # the provider's documented 14/90-day limits for aggregation endpoints.
    maximum = timedelta(days=14 if window.data_type == "heart-rate" else 90)
    start = window.start
    while start < window.end:
        end = min(start + maximum, window.end)
        yield Window(window.data_type, start, end, time_basis=window.time_basis)
        start = end


class HealthClient:
    """Return a snapshot only after every chunk and page has been validated.

    ``window`` is the actual cursor range to replace. Retries are completed by the
    acquisition runner, so a failed page never produces a snapshot.
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
        self.request_guard: Callable[[], float | None] | None = None

    def _request_timeout(self) -> float:
        remaining = self.request_guard() if self.request_guard is not None else None
        return self._timeout if remaining is None else min(self._timeout, remaining)

    def fetch_snapshot(self, window: Window, *, subject_key: str) -> Snapshot:
        if window.data_type == "heart-rate":
            raise ValueError("heart rate requires the minute aggregation API")
        snapshot = Snapshot(subject_key, window, self._clock(), ())
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
                raw_points = page.get("dataPoints", [])
                if not isinstance(raw_points, list):
                    raise InvalidResponseError("Google Health dataPoints must be an array")
                for raw_point in raw_points:
                    try:
                        point = object_dict(raw_point)
                        normalized = normalize(window.data_type, point, subject_key)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        raise InvalidResponseError("Malformed Google Health data point") from None
                    for record in normalized:
                        if not chunk.start <= chunk.record_cursor(record) < chunk.end:
                            raise InvalidResponseError("Google Health point is outside query range")
                        identity = (record.kind, record.record_id)
                        if identity in identities:
                            raise InvalidResponseError(
                                "Google Health returned a duplicate identity"
                            )
                        identities.add(identity)
                    records.extend(normalized)
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
            return replace(snapshot, records=tuple(records))
        except ValueError:
            raise InvalidResponseError(
                "Google Health records do not form a valid snapshot"
            ) from None

    def fetch_heart_rate_minutes(
        self, window: Window, *, subject_key: str
    ) -> HeartRateMinuteSnapshot:
        """Acquire all rollup pages over completed, aligned UTC minute windows."""
        if window.data_type != "heart-rate":
            raise ValueError("minute heart rate requires a heart-rate window")
        fetched_at = aware(self._clock())
        start = window.start.replace(second=0, microsecond=0)
        if start < window.start:
            start += timedelta(minutes=1)
        end = min(window.end, fetched_at).replace(second=0, microsecond=0)
        if start >= end:
            raise ValueError("heart rate range has no complete minute")
        effective = Window("heart-rate", start, end)
        minutes: list[HeartRateMinute] = []
        seen_starts: set[datetime] = set()
        page_count = 0
        deadline = time.monotonic() + 1200
        for chunk in _chunks(effective):
            page_token = ""
            seen_tokens: set[str] = set()
            while True:
                if page_count >= self._max_pages or time.monotonic() >= deadline:
                    raise InvalidResponseError("Google Health acquisition limit exceeded")
                page_count += 1
                page = self._rollup_page(chunk, page_token)
                points = page.get("rollupDataPoints", [])
                if not isinstance(points, list):
                    raise InvalidResponseError("Google Health rollupDataPoints must be an array")
                for value in points:
                    try:
                        point = object_dict(value)
                        left, right = (
                            parse_timestamp(point["startTime"]),
                            parse_timestamp(point["endTime"]),
                        )
                        if left.second or left.microsecond or right - left != timedelta(minutes=1):
                            raise ValueError("rollup window is not one UTC minute")
                        if not chunk.start <= left < right <= chunk.end:
                            raise ValueError("rollup window outside query range")
                        if left in seen_starts:
                            raise ValueError("duplicate rollup window")
                        seen_starts.add(left)
                        minute = normalize_heart_rate_minute(point, start=left, end=right)
                        if minute is None:
                            continue
                        minutes.append(minute)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        raise InvalidResponseError(
                            "Malformed Google Health heart rate rollup"
                        ) from None
                    if len(minutes) > 250_000:
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
        return HeartRateMinuteSnapshot(subject_key, effective, fetched_at, tuple(minutes))

    def _rollup_page(self, window: Window, page_token: str) -> dict[str, object]:
        token = self._access_token()
        if not isinstance(token, str) or not token.strip():
            raise AuthenticationError("Google Health access token is missing")
        body: dict[str, object] = {
            "range": {"startTime": utc_text(window.start), "endTime": utc_text(window.end)},
            "windowSize": "60s",
            "pageSize": 10_000,
            "dataSourceFamily": DATA_SOURCE_FAMILY,
        }
        if page_token:
            body["pageToken"] = page_token
        LOGGER.info("fitbit api_request endpoint=rollUp data_type=heart-rate count=1")
        return request_json(
            self._transport,
            "POST",
            "https://health.googleapis.com/v4/users/me/dataTypes/heart-rate/dataPoints:rollUp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body=json.dumps(body, separators=(",", ":")).encode(),
            timeout=self._request_timeout(),
        )

    def _page(self, window: Window, page_token: str) -> dict[str, object]:
        field = _FILTER_FIELDS[window.data_type]
        if window.data_type in DATE_TYPES or window.time_basis == "civil":
            start, end = window.start.date().isoformat(), window.end.date().isoformat()
            if window.time_basis == "civil":
                field = "steps.interval.civil_start_time"
        else:
            start, end = utc_text(window.start), utc_text(window.end)
        query: dict[str, str | int] = {
            "filter": f'{field} >= "{start}" AND {field} < "{end}"',
            "pageSize": 25 if window.data_type in ("sleep", "exercise") else 10_000,
            "dataSourceFamily": data_source_family_for(window.data_type),
        }
        if page_token:
            query["pageToken"] = page_token
        token = self._access_token()
        if not isinstance(token, str) or not token.strip():
            raise AuthenticationError("Google Health access token is missing")
        LOGGER.info("fitbit api_request endpoint=reconcile data_type=%s count=1", window.data_type)
        return request_json(
            self._transport,
            "GET",
            f"https://health.googleapis.com/v4/users/me/dataTypes/{window.data_type}"
            f"/dataPoints:reconcile?{urlencode(query)}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=self._request_timeout(),
        )
