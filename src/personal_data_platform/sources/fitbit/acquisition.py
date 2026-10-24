"""Bounded API acquisition and transactional range replacement."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from personal_data_platform.loader.deadline import interrupt_after
from personal_data_platform.network_diagnostics import network_phase
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConnectionError

from .api import HealthClient
from .models import (
    DATE_TYPES,
    AcquisitionMode,
    HeartRateMinuteSnapshot,
    Notification,
    Snapshot,
    Window,
)
from .writer import FitbitBatch, FitbitMinuteBatch, WriteResult

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AcquisitionSummary:
    completed_scopes: int = 0
    failed_scopes: int = 0
    deferred_scopes: int = 0
    first_incomplete: Window | None = None

    @property
    def ok(self) -> bool:
        return self.failed_scopes == self.deferred_scopes == 0


class AcquisitionDeferred(RuntimeError):
    """A budget, incomplete minute or ownership gate requires another execution."""


class AcquisitionClient(Protocol):
    def fetch_snapshot(self, window: Window, *, subject_key: str) -> Snapshot: ...
    def fetch_heart_rate_minutes(
        self, window: Window, *, subject_key: str
    ) -> HeartRateMinuteSnapshot: ...


def _log_acquisition(
    window: Window,
    *,
    status: str,
    notification_id: str | None,
    attempt_id: str | None,
    acquisition: Snapshot | HeartRateMinuteSnapshot | None = None,
    result: WriteResult | None = None,
    error: Exception | None = None,
    reason: str | None = None,
    acquisition_mode: AcquisitionMode | None = None,
) -> None:
    fetched = None
    if acquisition is not None:
        fetched = (
            len(acquisition.minutes)
            if isinstance(acquisition, HeartRateMinuteSnapshot)
            else len(acquisition.records)
        )
    LOGGER.log(
        logging.ERROR if status in ("failed", "unknown") else logging.INFO,
        "fitbit acquisition %s",
        status,
        extra={
            "event": "fitbit_acquisition",
            "status": status,
            "summary": {
                "notification_id": notification_id,
                "attempt_id": attempt_id,
                "acquisition_mode": acquisition_mode,
                "data_type": window.data_type,
                "time_basis": "civil" if window.data_type in DATE_TYPES else window.time_basis,
                "requested_start": window.start.isoformat(),
                "requested_end": window.end.isoformat(),
                "acquired_start": acquisition.window.start.isoformat() if acquisition else None,
                "acquired_end": acquisition.window.end.isoformat() if acquisition else None,
                "fetched_at": acquisition.fetched_at.isoformat() if acquisition else None,
                "fetched_rows": fetched,
                "submitted_rows": result.submitted_rows if result else None,
                "outcome": result.outcome if result else None,
                "accepted_ranges": [
                    {"start": start.isoformat(), "end": end.isoformat()}
                    for start, end in result.accepted_ranges
                ]
                if result
                else None,
                "protected_records": result.protected_records if result else None,
                "error_type": type(error).__name__ if error else None,
                "reason": reason,
            },
        },
    )


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

    def _remaining(self, deadline: float) -> float:
        seconds = deadline - self.monotonic()
        if seconds <= 0:
            raise AcquisitionDeferred("acquisition execution budget exhausted")
        return seconds

    def _check(self, warehouse: Warehouse, owner: str, deadline: float) -> float:
        seconds = self._remaining(deadline)
        try:
            warehouse.require_job_lock(owner, remaining_seconds=max(1, int(seconds)))
        except RuntimeError:
            raise AcquisitionDeferred("shared loader ownership lost") from None
        return seconds

    def process_notification(
        self,
        notification: Notification,
        *,
        attempt_id: str | None = None,
        warehouse: Warehouse,
        lease_owner: str,
        timeout_seconds: int = 480,
    ) -> AcquisitionSummary:
        """Acquire a notification scope; the caller acknowledges confirmed commits."""
        if notification.subject_key != self.subject_key:
            raise ValueError("notification subject does not match configured subject")
        if not 0 < timeout_seconds <= 6000:
            raise ValueError("invalid acquisition budget")
        mode = notification.acquisition_mode
        windows = (
            notification.windows
            if mode in ("bounded_minutes", "bounded_steps")
            else acquisition_windows(notification.windows)
        )
        if self.paused:
            for window in windows:
                _log_acquisition(
                    window,
                    status="deferred",
                    notification_id=notification.notification_id,
                    attempt_id=attempt_id,
                    reason="processing_paused",
                    acquisition_mode=mode,
                )
            return AcquisitionSummary(deferred_scopes=len(windows))
        if mode == "bounded_minutes" and any(
            window.end > self.clock().replace(second=0, microsecond=0) for window in windows
        ):
            for window in windows:
                _log_acquisition(
                    window,
                    status="deferred",
                    notification_id=notification.notification_id,
                    attempt_id=attempt_id,
                    reason="notified_utc_minute_incomplete",
                    acquisition_mode=mode,
                )
            return AcquisitionSummary(deferred_scopes=len(windows), first_incomplete=windows[0])
        return self.run_windows(
            windows,
            warehouse=warehouse,
            lease_owner=lease_owner,
            timeout_seconds=timeout_seconds,
            notification_id=notification.notification_id,
            attempt_id=attempt_id,
            acquisition_mode=mode,
            reserve_seconds=0,
        )

    def run_windows(
        self,
        windows: tuple[Window, ...],
        *,
        warehouse: Warehouse,
        lease_owner: str,
        timeout_seconds: int,
        notification_id: str | None = None,
        attempt_id: str | None = None,
        acquisition_mode: AcquisitionMode | None = None,
        reserve_seconds: float = 120,
    ) -> AcquisitionSummary:
        if self.paused:
            for window in windows:
                _log_acquisition(
                    window,
                    status="deferred",
                    notification_id=notification_id,
                    attempt_id=attempt_id,
                    reason="processing_paused",
                    acquisition_mode=acquisition_mode,
                )
            return AcquisitionSummary(
                deferred_scopes=len(windows), first_incomplete=windows[0] if windows else None
            )
        if not 0 < timeout_seconds <= 6000:
            raise ValueError("invalid acquisition budget")
        units = []
        for window in windows:
            start = window.start
            while start < window.end:
                zone = (
                    UTC
                    if window.data_type in DATE_TYPES or window.time_basis == "civil"
                    else ZoneInfo("Asia/Tokyo")
                )
                midnight = start.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
                end = min(window.end, (midnight + timedelta(days=1)).astimezone(UTC))
                units.append(Window(window.data_type, start, end, time_basis=window.time_basis))
                if len(units) > 50000:
                    raise ValueError("use a narrower manual range")
                start = end
        timer = interrupt_after(warehouse, timeout_seconds)
        try:
            return self._process(
                warehouse,
                lease_owner,
                tuple(sorted(set(units), key=lambda w: (w.start, w.data_type, w.time_basis))),
                self.monotonic() + timeout_seconds,
                notification_id=notification_id,
                attempt_id=attempt_id,
                acquisition_mode=acquisition_mode,
                reserve_seconds=reserve_seconds,
            )[0]
        finally:
            timer.cancel()
            timer.join()
            if isinstance(self.client, HealthClient):
                self.client.request_guard = None

    def _commit_acquisition(
        self,
        warehouse: Warehouse,
        acquisition: Snapshot | HeartRateMinuteSnapshot,
        owner: str,
        deadline: float,
    ) -> WriteResult:
        batch: FitbitBatch | FitbitMinuteBatch
        if isinstance(acquisition, HeartRateMinuteSnapshot):
            batch = FitbitMinuteBatch(acquisition)
        else:
            batch = FitbitBatch(acquisition)
        self._check(warehouse, owner, deadline)
        try:
            warehouse.connection.execute("BEGIN")
        except Exception:
            warehouse.connection_usable = False
            raise WarehouseConnectionError("cannot begin acquisition transaction") from None
        try:
            result = batch.write_snapshot(
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
        return result

    def _process(
        self,
        warehouse: Warehouse,
        owner: str,
        windows: tuple[Window, ...],
        deadline: float,
        *,
        notification_id: str | None = None,
        attempt_id: str | None = None,
        acquisition_mode: AcquisitionMode | None = None,
        reserve_seconds: float = 120,
    ) -> tuple[AcquisitionSummary, set[Window]]:
        remaining = self._remaining(deadline)
        failed = 0
        completed: set[Window] = set()
        incomplete: set[Window] = set()
        deferred = 0
        acquisition_deadline = deadline - min(reserve_seconds, remaining / 5)
        last_fetch_seconds = 0.0
        for index, window in enumerate(windows):
            acquisition: Snapshot | HeartRateMinuteSnapshot | None = None
            try:
                available = self._remaining(acquisition_deadline)
                if index >= 500 or (completed and available <= last_fetch_seconds):
                    raise AcquisitionDeferred("insufficient acquisition budget")
                if isinstance(self.client, HealthClient):
                    self.client.request_guard = lambda: self._check(
                        warehouse, owner, acquisition_deadline
                    )
                started = self.monotonic()
                fetched: Snapshot | HeartRateMinuteSnapshot
                with network_phase("fitbit_api"):
                    if window.data_type == "heart-rate":
                        if (
                            min(window.end, self.clock()).replace(second=0, microsecond=0)
                            <= window.start
                        ):
                            raise AcquisitionDeferred("no completed UTC minute")
                        fetched = self.client.fetch_heart_rate_minutes(
                            window, subject_key=self.subject_key
                        )
                    else:
                        fetched = self.client.fetch_snapshot(window, subject_key=self.subject_key)
                last_fetch_seconds = max(last_fetch_seconds, self.monotonic() - started)
                if (
                    fetched.subject_key != self.subject_key
                    or fetched.window.data_type != window.data_type
                    or not window.start <= fetched.window.start < fetched.window.end <= window.end
                ):
                    raise ValueError("API acquisition does not match requested window")
                acquisition = fetched
                if (
                    acquisition_mode in ("bounded_minutes", "bounded_steps")
                    and fetched.window != window
                ):
                    raise AcquisitionDeferred("notified acquisition range is incomplete")
                self._remaining(acquisition_deadline)
                result = self._commit_acquisition(warehouse, acquisition, owner, deadline)
                completed.add(window)
                _log_acquisition(
                    window,
                    status="succeeded",
                    notification_id=notification_id,
                    attempt_id=attempt_id,
                    acquisition=acquisition,
                    result=result,
                    acquisition_mode=acquisition_mode,
                )
            except AcquisitionDeferred as error:
                deferred += 1
                incomplete.add(window)
                _log_acquisition(
                    window,
                    status="deferred",
                    notification_id=notification_id,
                    attempt_id=attempt_id,
                    acquisition=acquisition,
                    error=error,
                    reason=str(error),
                    acquisition_mode=acquisition_mode,
                )
            except WarehouseConnectionError as error:
                _log_acquisition(
                    window,
                    status="unknown",
                    notification_id=notification_id,
                    attempt_id=attempt_id,
                    acquisition=acquisition,
                    error=error,
                    acquisition_mode=acquisition_mode,
                )
                raise
            except Exception as error:
                failed += 1
                incomplete.add(window)
                _log_acquisition(
                    window,
                    status="failed",
                    notification_id=notification_id,
                    attempt_id=attempt_id,
                    acquisition=acquisition,
                    error=error,
                    acquisition_mode=acquisition_mode,
                )
                LOGGER.error(
                    "fitbit scope failed data_type=%s error_type=%s; use manual range sync if persistent",
                    window.data_type,
                    type(error).__name__,
                )
        first = min(incomplete, key=lambda w: (w.start, w.data_type)) if incomplete else None
        return AcquisitionSummary(
            len(completed), failed, deferred, first_incomplete=first
        ), completed
