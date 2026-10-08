"""Bounded API acquisition and transactional range replacement."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from personal_data_platform.loader.deadline import interrupt_after
from personal_data_platform.loader.job import LOADER_LEASE_SECONDS
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConnectionError

from .api import HealthClient
from .models import (
    DATE_TYPES,
    CapturedSnapshot,
    HeartRateMinuteSnapshot,
    Window,
)
from .notifications import Delivery
from .writer import FitbitBatch, FitbitMinuteBatch

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AcquisitionSummary:
    completed_scopes: int = 0
    failed_scopes: int = 0
    deferred_scopes: int = 0
    acked_notifications: int = 0
    first_incomplete: Window | None = None

    @property
    def ok(self) -> bool:
        return self.failed_scopes == self.deferred_scopes == 0


class AcquisitionDeferred(RuntimeError):
    """A budget, incomplete minute or ownership gate requires another execution."""


class NotificationQueue(Protocol):
    def pull(self, *, limit: int, timeout_seconds: float) -> tuple[Delivery, ...]: ...
    def extend(self, ack_ids: tuple[str, ...], *, seconds: int) -> None: ...
    def ack(self, ack_ids: tuple[str, ...]) -> None: ...


class AcquisitionClient(Protocol):
    def fetch_captured(self, window: Window, *, subject_key: str) -> CapturedSnapshot: ...
    def fetch_heart_rate_minutes(
        self, window: Window, *, subject_key: str
    ) -> HeartRateMinuteSnapshot: ...


def acquisition_windows(windows: tuple[Window, ...]) -> tuple[Window, ...]:
    """Coalesce physical scopes by Tokyo day; provider dates retain their own calendar."""
    result: set[Window] = set()
    for window in windows:
        if window.data_type in DATE_TYPES:
            start = window.start
            end = window.end
        else:
            local = window.start.astimezone(ZoneInfo("Asia/Tokyo"))
            start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
            end = window.end
        while start < end:
            stop = start + timedelta(days=1)
            result.add(Window(window.data_type, start, stop))
            if len(result) > 50_000:
                raise ValueError("notification range requires explicit bounded backfill")
            start = stop
    return tuple(sorted(result, key=lambda value: (value.start, value.data_type)))


class _AckLease:
    def __init__(self, queue: NotificationQueue, deliveries: list[Delivery]) -> None:
        self.queue, self.deliveries = queue, deliveries
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def extend(self) -> None:
        ids = tuple(delivery.ack_id for delivery in self.deliveries)
        try:
            self.queue.extend(ids, seconds=600)
        except Exception as error:
            LOGGER.warning("fitbit ack extension failed error_type=%s", type(error).__name__)

    def _run(self) -> None:
        while not self.stop.wait(30):
            self.extend()

    def __enter__(self) -> _AckLease:
        self.extend()
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop.set()
        self.thread.join(timeout=25)


class AcquisitionRunner:
    def __init__(
        self,
        *,
        client: AcquisitionClient,
        warehouse_factory: Callable[[], Warehouse],
        subject_key: str,
        paused: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client, self.warehouse_factory = client, warehouse_factory
        self.subject_key, self.paused, self.clock, self.monotonic = (
            subject_key,
            paused,
            clock,
            monotonic,
        )

    def _check(self, warehouse: Warehouse, owner: str, deadline: float) -> float:
        seconds = deadline - self.monotonic()
        if seconds <= 0:
            raise AcquisitionDeferred("acquisition execution budget exhausted")
        try:
            warehouse.require_job_lock(owner, remaining_seconds=max(1, int(seconds)))
        except RuntimeError:
            raise AcquisitionDeferred("shared loader ownership lost") from None
        return seconds

    def ingest(
        self,
        queue: NotificationQueue,
        *,
        max_messages: int = 500,
        collect_seconds: int = 120,
        timeout_seconds: int = 3000,
    ) -> AcquisitionSummary:
        if (
            not 1 <= max_messages <= 500
            or not 0 <= collect_seconds <= 120
            or not 0 < timeout_seconds <= 3000
        ):
            raise ValueError("invalid notification job limits")
        deadline = self.monotonic() + timeout_seconds
        collect_deadline = min(deadline, self.monotonic() + collect_seconds)
        deliveries = list(
            queue.pull(
                limit=min(50, max_messages),
                timeout_seconds=min(30, collect_seconds or 1, deadline - self.monotonic()),
            )
        )
        while (
            not deliveries
            and not getattr(queue, "invalid_count", 0)
            and self.monotonic() < collect_deadline
        ):
            time.sleep(min(1, max(0, collect_deadline - self.monotonic())))
            seconds = collect_deadline - self.monotonic()
            if seconds <= 0:
                break
            deliveries.extend(
                queue.pull(limit=min(50, max_messages), timeout_seconds=min(30, seconds))
            )
        if not deliveries:
            return AcquisitionSummary(failed_scopes=int(bool(getattr(queue, "invalid_count", 0))))
        if self.paused:
            queue.extend(tuple(d.ack_id for d in deliveries), seconds=0)
            return AcquisitionSummary(deferred_scopes=len(deliveries))
        warehouse = self.warehouse_factory()
        owner = uuid.uuid4().hex
        acquired = False
        timer = None
        try:
            warehouse.migrate()
            acquired = warehouse.acquire_job_lock(
                "loader", owner, lease_seconds=LOADER_LEASE_SECONDS
            )
            if not acquired:
                queue.extend(tuple(d.ack_id for d in deliveries), seconds=0)
                return AcquisitionSummary(deferred_scopes=len(deliveries))
            timer = interrupt_after(warehouse, self._check(warehouse, owner, deadline))
            with _AckLease(queue, deliveries):
                while len(deliveries) < max_messages and self.monotonic() < collect_deadline:
                    more = queue.pull(
                        limit=min(50, max_messages - len(deliveries)),
                        timeout_seconds=min(30, max(0.01, collect_deadline - self.monotonic())),
                    )
                    deliveries.extend(more)
                    if not more:
                        break
                valid = [d for d in deliveries if d.notification.subject_key == self.subject_key]
                windows = acquisition_windows(
                    tuple(w for d in valid for w in d.notification.windows)
                )
                try:
                    result, completed = self._process(warehouse, owner, windows, deadline)
                except WarehouseConnectionError:
                    return AcquisitionSummary(failed_scopes=len(windows))
                ack_ids = tuple(
                    d.ack_id
                    for d in valid
                    if all(w in completed for w in acquisition_windows(d.notification.windows))
                )
                acked = 0
                failures = (
                    result.failed_scopes
                    + len(deliveries)
                    - len(valid)
                    + int(getattr(queue, "invalid_count", 0))
                )
                try:
                    queue.ack(ack_ids)
                    acked = len(ack_ids)
                except Exception as error:
                    failures += 1
                    LOGGER.warning("fitbit ack failed error_type=%s", type(error).__name__)
                pending = tuple(
                    d.ack_id for d in deliveries if not acked or d.ack_id not in ack_ids
                )
                if pending:
                    queue.extend(pending, seconds=0)
                return AcquisitionSummary(
                    result.completed_scopes,
                    failures,
                    result.deferred_scopes,
                    acked,
                    result.first_incomplete,
                )
        finally:
            if timer is not None:
                timer.cancel()
            if isinstance(self.client, HealthClient):
                self.client.request_guard = None
            if acquired and warehouse.connection_usable:
                warehouse.release_job_lock("loader", owner)
            warehouse.close()

    def run_windows(
        self,
        windows: tuple[Window, ...],
        *,
        warehouse: Warehouse,
        lease_owner: str,
        timeout_seconds: int,
    ) -> AcquisitionSummary:
        if self.paused:
            return AcquisitionSummary(
                deferred_scopes=len(windows), first_incomplete=windows[0] if windows else None
            )
        if not 0 < timeout_seconds <= 6000:
            raise ValueError("invalid acquisition budget")
        units = []
        for window in windows:
            start = window.start
            while start < window.end:
                zone = UTC if window.data_type in DATE_TYPES else ZoneInfo("Asia/Tokyo")
                midnight = start.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
                end = min(window.end, (midnight + timedelta(days=1)).astimezone(UTC))
                units.append(Window(window.data_type, start, end))
                if len(units) > 50000:
                    raise ValueError("use a narrower manual range")
                start = end
        timer = interrupt_after(warehouse, timeout_seconds)
        try:
            return self._process(
                warehouse,
                lease_owner,
                tuple(sorted(set(units), key=lambda w: (w.start, w.data_type))),
                self.monotonic() + timeout_seconds,
            )[0]
        finally:
            timer.cancel()
            if isinstance(self.client, HealthClient):
                self.client.request_guard = None

    def _commit_acquisition(
        self,
        warehouse: Warehouse,
        acquisition: CapturedSnapshot | HeartRateMinuteSnapshot,
        owner: str,
        deadline: float,
    ) -> None:
        batch: FitbitBatch | FitbitMinuteBatch
        if isinstance(acquisition, HeartRateMinuteSnapshot):
            batch = FitbitMinuteBatch(acquisition)
        else:
            batch = FitbitBatch(acquisition.snapshot, source_digest=acquisition.source_sha256())
        self._check(warehouse, owner, deadline)
        try:
            warehouse.connection.execute("BEGIN")
        except Exception:
            warehouse.connection_usable = False
            raise WarehouseConnectionError("cannot begin acquisition transaction") from None
        try:
            batch.write_snapshot(
                warehouse.connection,
                source_key="fitbit-api:" + uuid.uuid4().hex,
                loaded_at=self.clock(),
            )
            self._check(warehouse, owner, deadline)
        except Exception:
            try:
                warehouse.connection.execute("ROLLBACK")
            except Exception:
                warehouse.connection_usable = False
                raise WarehouseConnectionError("acquisition rollback outcome unknown") from None
            raise
        try:
            warehouse.connection.execute("COMMIT")
        except Exception:
            warehouse.connection_usable = False
            raise WarehouseConnectionError("acquisition commit outcome unknown") from None

    def _process(
        self, warehouse: Warehouse, owner: str, windows: tuple[Window, ...], deadline: float
    ) -> tuple[AcquisitionSummary, set[Window]]:
        self._check(warehouse, owner, deadline)
        failed = 0
        completed: set[Window] = set()
        incomplete: set[Window] = set()
        deferred = 0
        acquisition_deadline = deadline - min(120.0, self._check(warehouse, owner, deadline) / 5)
        last_fetch_seconds = 0.0
        for index, window in enumerate(windows):
            try:
                available = self._check(warehouse, owner, acquisition_deadline)
                if index >= 500 or (completed and available <= last_fetch_seconds):
                    raise AcquisitionDeferred("insufficient acquisition budget")
                if isinstance(self.client, HealthClient):
                    self.client.request_guard = lambda: self._check(
                        warehouse, owner, acquisition_deadline
                    )
                started = self.monotonic()
                if window.data_type == "heart-rate":
                    if (
                        min(window.end, self.clock()).replace(second=0, microsecond=0)
                        <= window.start
                    ):
                        raise AcquisitionDeferred("no completed UTC minute")
                    acquisition: CapturedSnapshot | HeartRateMinuteSnapshot = (
                        self.client.fetch_heart_rate_minutes(window, subject_key=self.subject_key)
                    )
                else:
                    acquisition = self.client.fetch_captured(window, subject_key=self.subject_key)
                last_fetch_seconds = max(last_fetch_seconds, self.monotonic() - started)
                if (
                    acquisition.subject_key != self.subject_key
                    or acquisition.window.data_type != window.data_type
                    or not window.start
                    <= acquisition.window.start
                    < acquisition.window.end
                    <= window.end
                ):
                    raise ValueError("API acquisition does not match requested window")
                self._check(warehouse, owner, acquisition_deadline)
                self._commit_acquisition(warehouse, acquisition, owner, deadline)
                completed.add(window)
            except AcquisitionDeferred:
                deferred += 1
                incomplete.add(window)
            except WarehouseConnectionError:
                raise
            except Exception as error:
                failed += 1
                incomplete.add(window)
                LOGGER.error(
                    "fitbit scope failed data_type=%s error_type=%s; use manual range sync if persistent",
                    window.data_type,
                    type(error).__name__,
                )
        first = min(incomplete, key=lambda w: (w.start, w.data_type)) if incomplete else None
        return AcquisitionSummary(
            len(completed), failed, deferred, first_incomplete=first
        ), completed
