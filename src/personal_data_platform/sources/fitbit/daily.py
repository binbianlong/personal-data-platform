"""Bounded daily API acquisition through the shared Raw and Loader contracts."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import ceil
from typing import Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from personal_data_platform.loader.job import LOADER_LEASE_SECONDS, run_loader_objects
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository
from personal_data_platform.storage.motherduck import Warehouse

from .adapter import FitbitSource
from .api import SyncTime
from .models import DATA_TYPES, DATE_TYPES, Snapshot, Window, date_cursor
from .raw import SnapshotBundle, SnapshotRepository, encode_bundle
from .state import CheckedWindow, DailyState, DailyStateStore, PendingBatch
from .writer import can_skip_snapshot, expand_window

LOGGER = logging.getLogger(__name__)
LOOKBACK_DAYS = BUNDLE_DAYS = 7
MAX_DAILY_DAYS = 90
TOKYO = ZoneInfo("Asia/Tokyo")


class Store(RawRepository, SnapshotRepository, Protocol):
    pass


class Client(Protocol):
    def fetch(self, window: Window, *, subject_key: str) -> Snapshot: ...
    def latest_tracker_sync(self) -> SyncTime | None: ...


@dataclass(slots=True)
class DailySummary:
    status: Literal["succeeded", "paused", "deferred"] = "succeeded"
    fetched_windows: int = 0
    raw_saved: int = 0
    raw_skipped: int = 0
    recovered: int = 0
    compressed_bytes: int = 0
    full_success_age_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class _BufferedRaw:
    raw: RawObject
    compressed: bytes

    def list_raw(self, prefix: str) -> tuple[RawObject, ...]:
        raise RuntimeError("buffered targeted ingestion must not list GCS")

    def get_raw(self, key: str, *, generation: int) -> bytes:
        if (key, generation) != (self.raw.key, self.raw.storage_generation):
            raise ValueError("buffered Raw identity changed")
        return self.compressed


def sync_windows(start: datetime, end: datetime, data_types: tuple[str, ...]) -> tuple[Window, ...]:
    windows = []
    for kind in data_types:
        if kind in DATE_TYPES:
            first = start.astimezone(TOKYO).date()
            last = end.astimezone(TOKYO)
            stop = date_cursor(last.date())
            if last.time() != time():
                stop += timedelta(days=1)
            windows.append(Window(kind, date_cursor(first), stop))
        else:
            windows.append(Window(kind, start, end))
    return tuple(windows)


def _day_windows(first: date, stop: date) -> tuple[Window, ...]:
    return tuple(
        window
        for index in range((stop - first).days)
        for window in sync_windows(
            datetime.combine(first + timedelta(days=index), time(), TOKYO),
            datetime.combine(first + timedelta(days=index + 1), time(), TOKYO),
            DATA_TYPES,
        )
    )


def _range_batches(windows: tuple[Window, ...]) -> Iterator[tuple[Window, ...]]:
    days = max(
        (ceil((window.end - window.start) / timedelta(days=1)) for window in windows), default=0
    )
    for offset in range(0, days, BUNDLE_DAYS):
        yield tuple(
            Window(window.data_type, start, min(start + timedelta(days=1), window.end))
            for window in windows
            for day in range(offset, offset + BUNDLE_DAYS)
            for start in (window.start + timedelta(days=day),)
            if start < window.end
        )


def _expanded(
    warehouse: Warehouse, subject_key: str, windows: tuple[Window, ...]
) -> tuple[Window, ...]:
    result: list[Window] = []
    for kind in DATA_TYPES:
        values = sorted(
            (
                expand_window(warehouse, subject_key, window)
                for window in windows
                if window.data_type == kind
            ),
            key=lambda value: value.start,
        )
        for window in values:
            if result and result[-1].data_type == kind and result[-1].end > window.start:
                previous = result.pop()
                window = Window(kind, previous.start, max(previous.end, window.end))
            result.append(window)
    return tuple(result)


def _load(warehouse: Warehouse, owner: str, raw: RawObject, compressed: bytes) -> None:
    summary = run_loader_objects(
        _BufferedRaw(raw, compressed), warehouse, (raw,), source=FitbitSource(), _lease_owner=owner
    )
    if not summary.ok:
        raise RuntimeError("Fitbit bundled Raw load failed")


def _capture(
    repository: Store,
    warehouse: Warehouse,
    client: Client,
    state: DailyStateStore,
    owner: str,
    windows: tuple[Window, ...],
    progress: DailyState,
    summary: DailySummary,
    *,
    supersedes: tuple[str, ...] = (),
    minimum_fetched_at: datetime | None = None,
) -> None:
    changed: list[Snapshot] = []
    skipped: list[CheckedWindow] = []
    for window in windows:
        snapshot = client.fetch(window, subject_key=state.subject_key)
        if snapshot.subject_key != state.subject_key or snapshot.window != window:
            raise ValueError("Fitbit client returned a different acquisition scope")
        if minimum_fetched_at is not None and snapshot.fetched_at <= minimum_fetched_at:
            raise RuntimeError("recovery acquisition must be newer than its unresolved Raw")
        summary.fetched_windows += 1
        if can_skip_snapshot(warehouse, snapshot):
            skipped.append(CheckedWindow(window, snapshot.fetched_at, snapshot.source_sha256()))
        else:
            changed.append(snapshot)
    summary.raw_skipped += len(skipped)
    if not changed:
        state.finish(progress, tuple(skipped), retired_keys=supersedes)
        return
    key, compressed = encode_bundle(SnapshotBundle(tuple(changed)))
    pending = PendingBatch(key, windows, tuple(skipped), progress, supersedes)
    state.prepare(pending)
    raw = repository.put_raw_object(key, compressed)
    if raw.key != key:
        raise ValueError("Fitbit upload returned a different Raw key")
    summary.raw_saved += 1
    summary.compressed_bytes += len(compressed)
    _load(warehouse, owner, raw, compressed)
    state.finish(progress, pending.skipped, retired_keys=(key, *supersedes))


def _recover(
    repository: Store,
    warehouse: Warehouse,
    client: Client,
    state: DailyStateStore,
    owner: str,
    summary: DailySummary,
) -> None:
    source = FitbitSource()
    for pending in state.pending():
        rows = warehouse.query_rows(
            "SELECT storage_created_at, storage_generation FROM ops.ingestion_metadata "
            "WHERE object_key=? AND status='succeeded' AND parser_version=? "
            "AND retention_expired_at IS NULL",
            [pending.raw_key, source.parser_version],
        )
        raw = (
            source.parse_raw_key(
                pending.raw_key, storage_created_at=rows[0][0], storage_generation=int(rows[0][1])
            )
            if rows
            else None
        )
        if raw is not None and raw.key in warehouse.succeeded_keys_for(
            (raw,), parser_version=source.parser_version
        ):
            state.finish(
                pending.progress,
                pending.skipped,
                retired_keys=(pending.raw_key, *pending.supersedes),
            )
        else:
            raw = repository.head_raw(pending.raw_key)
            if raw is not None:
                compressed = repository.get_raw(raw.key, generation=raw.storage_generation)
                _load(warehouse, owner, raw, compressed)
                state.finish(
                    pending.progress,
                    pending.skipped,
                    retired_keys=(pending.raw_key, *pending.supersedes),
                )
            else:
                expected = source.parse_raw_key(
                    pending.raw_key, storage_created_at=datetime.now(UTC), storage_generation=1
                )
                _capture(
                    repository,
                    warehouse,
                    client,
                    state,
                    owner,
                    pending.windows,
                    pending.progress,
                    summary,
                    supersedes=(pending.raw_key, *pending.supersedes),
                    minimum_fetched_at=expected.observed_at,
                )
        summary.recovered += 1
    # Legacy v1 uploads use the same ingestion ledger and exact-key recovery.
    for key, kind, start, end, fetched in warehouse.query_rows(
        "SELECT raw_key, data_type, range_start, range_end, fetched_at "
        "FROM ops.fitbit_raw_intent WHERE subject_key=? ORDER BY fetched_at, raw_key LIMIT 90",
        [state.subject_key],
    ):
        raw = repository.head_raw(key)
        if raw is not None:
            _load(warehouse, owner, raw, repository.get_raw(key, generation=raw.storage_generation))
        else:
            _capture(
                repository,
                warehouse,
                client,
                state,
                owner,
                (Window(kind, start, end),),
                DailyState(state.subject_key),
                summary,
                minimum_fetched_at=fetched,
            )
        summary.recovered += 1


def collect_daily(
    repository: Store,
    warehouse: Warehouse,
    *,
    client: Client,
    subject_key: str,
    now: datetime | None = None,
    paused: bool = False,
) -> DailySummary:
    if paused:
        return DailySummary(status="paused")
    started = now or datetime.now(UTC)
    today = started.astimezone(TOKYO).date()
    owner = str(uuid4())
    if not warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS):
        return DailySummary(status="deferred")
    summary = DailySummary()
    state = DailyStateStore(warehouse, subject_key)
    try:
        _recover(repository, warehouse, client, state, owner, summary)
        prior = state.read()
        if prior.last_daily_run is not None and prior.last_daily_run >= today:
            return summary
        latest = client.latest_tracker_sync()
        if latest is None:
            raise RuntimeError("no paired tracker sync time is available")
        if latest > SyncTime.from_datetime(started + timedelta(minutes=5)):
            raise ValueError("tracker sync time is in the future")
        first = today - timedelta(days=LOOKBACK_DAYS)
        if prior.completed_through is not None:
            first = min(first, prior.completed_through + timedelta(days=1))
        if prior.last_sync_time is not None and latest > prior.last_sync_time:
            first = min(first, prior.sync_covered_through or prior.last_sync_time.tokyo_date())
        stop = min(today, first + timedelta(days=MAX_DAILY_DAYS))
        day = first
        while day < stop:
            batch_stop = min(stop, day + timedelta(days=BUNDLE_DAYS))
            final = batch_stop == today
            progress = DailyState(
                subject_key,
                batch_stop - timedelta(days=1),
                min(batch_stop - timedelta(days=1), latest.tokyo_date()),
                latest if final else None,
                today if final else None,
            )
            _capture(
                repository,
                warehouse,
                client,
                state,
                owner,
                _expanded(warehouse, subject_key, _day_windows(day, batch_stop)),
                progress,
                summary,
            )
            day = batch_stop
        if stop < today:
            summary.status = "deferred"
        return summary
    finally:
        if warehouse.connection_usable:
            warehouse.release_job_lock("loader", owner)


def collect_range(
    repository: Store,
    warehouse: Warehouse,
    *,
    client: Client,
    subject_key: str,
    windows: tuple[Window, ...],
    paused: bool = False,
) -> DailySummary:
    if paused:
        return DailySummary(status="paused")
    owner = str(uuid4())
    if not warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS):
        return DailySummary(status="deferred")
    summary = DailySummary()
    state = DailyStateStore(warehouse, subject_key)
    try:
        _recover(repository, warehouse, client, state, owner, summary)
        for batch in _range_batches(windows):
            _capture(
                repository,
                warehouse,
                client,
                state,
                owner,
                _expanded(warehouse, subject_key, batch),
                DailyState(subject_key),
                summary,
            )
        return summary
    finally:
        if warehouse.connection_usable:
            warehouse.release_job_lock("loader", owner)
