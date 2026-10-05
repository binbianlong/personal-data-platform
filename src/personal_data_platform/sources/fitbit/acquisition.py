"""Bounded complete acquisitions, write-ahead bundles and commit-before-ack delivery."""

from __future__ import annotations

import hashlib
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
from personal_data_platform.loader.job import LOADER_LEASE_SECONDS, run_loader_objects
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConnectionError

from .acquisition_state import AcquisitionState
from .adapter import FitbitSource
from .api import HealthClient
from .models import (
    DATE_TYPES,
    AcquisitionScope,
    BundleEntry,
    CapturedSnapshot,
    FitbitBundle,
    HeartRateMinuteSnapshot,
    Notification,
    Window,
)
from .notifications import Delivery
from .raw import encode_bundle

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AcquisitionSummary:
    completed_scopes: int = 0
    failed_scopes: int = 0
    deferred_scopes: int = 0
    acked_notifications: int = 0

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


class AcquisitionRepository(RawRepository, Protocol):
    def put_raw_object(self, key: str, compressed_bytes: bytes) -> RawObject: ...
    def head_raw(self, key: str) -> RawObject | None: ...


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
        repository: AcquisitionRepository,
        client: AcquisitionClient,
        warehouse_factory: Callable[[], Warehouse],
        subject_key: str,
        paused: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repository, self.client, self.warehouse_factory = repository, client, warehouse_factory
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
        deliveries = list(queue.pull(limit=min(50, max_messages), timeout_seconds=1))
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
            acquired = warehouse.acquire_job_lock(
                "loader", owner, lease_seconds=LOADER_LEASE_SECONDS
            )
            if not acquired:
                queue.extend(tuple(d.ack_id for d in deliveries), seconds=0)
                return AcquisitionSummary(deferred_scopes=len(deliveries))
            timer = interrupt_after(warehouse, self._check(warehouse, owner, deadline))
            with _AckLease(queue, deliveries) as lease:
                collect_deadline = min(deadline, self.monotonic() + collect_seconds)
                while len(deliveries) < max_messages and self.monotonic() < collect_deadline:
                    more = queue.pull(
                        limit=min(50, max_messages - len(deliveries)),
                        timeout_seconds=min(30, max(0.01, collect_deadline - self.monotonic())),
                    )
                    deliveries.extend(more)
                    lease.extend()
                    if not more:
                        break
                notifications = []
                rejected = 0
                for delivery in deliveries:
                    if delivery.notification.subject_key != self.subject_key:
                        rejected += 1
                        continue
                    value = delivery.notification
                    notifications.append(
                        Notification(
                            value.notification_id,
                            value.subject_key,
                            acquisition_windows(value.windows),
                            value.received_at,
                        )
                    )
                try:
                    result = self._process(warehouse, owner, tuple(notifications), deadline)
                except WarehouseConnectionError:
                    timer.cancel()
                    warehouse.close()
                    warehouse = self.warehouse_factory()
                    timer = interrupt_after(warehouse, self._check(warehouse, owner, deadline))
                    ackable = AcquisitionState(warehouse).ackable_ids(
                        tuple(n.notification_id for n in notifications)
                    )
                    result = AcquisitionSummary(
                        completed_scopes=len(ackable),
                        failed_scopes=len(set(n.notification_id for n in notifications) - ackable),
                    )
                ackable = AcquisitionState(warehouse).ackable_ids(
                    tuple(n.notification_id for n in notifications)
                )
                ack_ids = tuple(
                    delivery.ack_id
                    for delivery in deliveries
                    if delivery.notification.notification_id in ackable
                )
                acked = 0
                try:
                    queue.ack(ack_ids)
                    acked = len(ack_ids)
                except Exception as error:
                    LOGGER.warning("fitbit ack failed error_type=%s", type(error).__name__)
                    rejected += len(ack_ids)
                pending_ids = tuple(
                    delivery.ack_id for delivery in deliveries if delivery.ack_id not in ack_ids
                )
                if pending_ids:
                    try:
                        queue.extend(pending_ids, seconds=0)
                    except Exception:
                        LOGGER.warning("fitbit pending notification release failed")
                return AcquisitionSummary(
                    result.completed_scopes,
                    result.failed_scopes + rejected,
                    result.deferred_scopes,
                    acked,
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
            return AcquisitionSummary(deferred_scopes=len(windows))
        if timeout_seconds <= 0 or timeout_seconds > 6000:
            raise ValueError("invalid acquisition budget")
        notification = Notification(
            uuid.uuid4().hex, self.subject_key, acquisition_windows(windows), self.clock()
        )
        timer = interrupt_after(warehouse, timeout_seconds)
        try:
            return self._process(
                warehouse, lease_owner, (notification,), self.monotonic() + timeout_seconds
            )
        finally:
            timer.cancel()
            if isinstance(self.client, HealthClient):
                self.client.request_guard = None

    def _recover(
        self, state: AcquisitionState, warehouse: Warehouse, owner: str, deadline: float
    ) -> None:
        for intent in state.pending_bundles():
            self._check(warehouse, owner, deadline)
            refs = []
            for key, _, _ in intent.chunks:
                raw = self.repository.head_raw(key)
                if raw is None:
                    break
                state.mark_chunk_saved(key, raw.storage_generation)
                refs.append(raw)
            else:
                self._check(warehouse, owner, deadline)
                summary = run_loader_objects(
                    self.repository,
                    warehouse,
                    refs,
                    source=FitbitSource(version=2),
                    _lease_owner=owner,
                    _deadline=time.monotonic() + self._check(warehouse, owner, deadline),
                )
                if not summary.ok:
                    LOGGER.error("fitbit bundle recovery failed bundle_id=%s", intent.bundle_id)

    def _process(
        self,
        warehouse: Warehouse,
        owner: str,
        notifications: tuple[Notification, ...],
        deadline: float,
    ) -> AcquisitionSummary:
        self._check(warehouse, owner, deadline)
        state = AcquisitionState(warehouse)
        scopes_by_id = state.register_notifications(notifications)
        state.retire_superseded_bundles()
        self._recover(state, warehouse, owner, deadline)
        grouped: dict[AcquisitionScope, list[str]] = {}
        for notification_id, scopes in scopes_by_id.items():
            for scope in scopes:
                pending = warehouse.query_value(
                    """SELECT count(*) FROM ops.fitbit_notification_scope ns
                    LEFT JOIN ops.fitbit_attempt a USING(attempt_id)
                    WHERE notification_id=? AND ns.scope_key=? AND a.status IS DISTINCT FROM 'succeeded' """,
                    [notification_id, scope.key],
                )
                if pending:
                    grouped.setdefault(scope, []).append(notification_id)
        changed: list[BundleEntry] = []
        completed = failed = deferred = 0
        # Leave time to commit the acquired prefix before the absolute job limit.
        acquisition_deadline = deadline - min(120.0, self._check(warehouse, owner, deadline) / 5)
        last_fetch_seconds = 0.0
        for index, (scope, ids) in enumerate(grouped.items()):
            try:
                if index >= 500:
                    raise AcquisitionDeferred("scope count budget exhausted")
                available = self._check(warehouse, owner, acquisition_deadline)
                if changed and available <= last_fetch_seconds:
                    raise AcquisitionDeferred("remaining budget reserved for bundle commit")
                started_at = self.clock()
                attempt = state.start_attempt(scope, started_at=started_at)
                waiting = warehouse.query_rows(
                    """SELECT ns.notification_id FROM ops.fitbit_notification_scope ns
                    JOIN ops.fitbit_notification n USING(notification_id)
                    LEFT JOIN ops.fitbit_attempt a USING(attempt_id)
                    WHERE ns.scope_key=? AND n.received_at<=? AND a.status IS DISTINCT FROM 'succeeded'""",
                    [scope.key, started_at],
                )
                state.bind_attempt(tuple(row[0] for row in waiting), scope, attempt)
                if isinstance(self.client, HealthClient):
                    self.client.request_guard = lambda: self._check(
                        warehouse, owner, acquisition_deadline
                    )
                fetch_started = self.monotonic()
                if scope.window.data_type == "heart-rate":
                    if (
                        min(scope.window.end, started_at).replace(second=0, microsecond=0)
                        <= scope.window.start
                    ):
                        raise AcquisitionDeferred("heart rate range has no complete minute")
                    acquisition: CapturedSnapshot | HeartRateMinuteSnapshot = (
                        self.client.fetch_heart_rate_minutes(
                            scope.window, subject_key=scope.subject_key
                        )
                    )
                else:
                    acquisition = self.client.fetch_captured(
                        scope.window, subject_key=scope.subject_key
                    )
                entry = BundleEntry(attempt, acquisition, scope)
                last_fetch_seconds = max(last_fetch_seconds, self.monotonic() - fetch_started)
                self._check(warehouse, owner, acquisition_deadline)
                previous = state.latest_success(scope)
                if previous is not None and previous.source_sha256 == acquisition.source_sha256():
                    warehouse.connection.execute("BEGIN")
                    try:
                        state.finish_attempt(
                            attempt,
                            source_sha256=previous.source_sha256,
                            raw_keys=previous.raw_keys,
                        )
                        warehouse.connection.execute("COMMIT")
                    except Exception:
                        warehouse.connection_usable = False
                        raise WarehouseConnectionError(
                            "unchanged commit outcome requires verification"
                        ) from None
                    completed += 1
                else:
                    changed.append(entry)
            except AcquisitionDeferred:
                deferred += 1
            except WarehouseConnectionError:
                raise
            except Exception as error:
                if self.monotonic() >= acquisition_deadline:
                    deferred += 1
                    continue
                failed += 1
                LOGGER.error(
                    "fitbit scope failed data_type=%s error_type=%s",
                    scope.window.data_type,
                    type(error).__name__,
                )
        if changed:
            bundle = FitbitBundle(uuid.uuid4().hex, tuple(changed))
            chunks = encode_bundle(bundle)
            self._check(warehouse, owner, deadline)
            warehouse.connection.execute("BEGIN")
            try:
                state.prepare_bundle(
                    bundle.bundle_id,
                    tuple(entry.attempt_id for entry in changed),
                    tuple(
                        (key, hashlib.sha256(payload).hexdigest(), len(payload))
                        for key, payload in chunks
                    ),
                )
                warehouse.connection.execute("COMMIT")
            except Exception:
                warehouse.connection_usable = False
                raise WarehouseConnectionError(
                    "bundle intent commit outcome requires verification"
                ) from None
            refs = []
            try:
                for key, payload in chunks:
                    self._check(warehouse, owner, deadline)
                    raw = self.repository.put_raw_object(key, payload)
                    state.mark_chunk_saved(key, raw.storage_generation)
                    refs.append(raw)
                self._check(warehouse, owner, deadline)
                summary = run_loader_objects(
                    self.repository,
                    warehouse,
                    refs,
                    source=FitbitSource(version=2),
                    _lease_owner=owner,
                    buffered_payloads=dict(chunks),
                    _deadline=time.monotonic() + self._check(warehouse, owner, deadline),
                )
                if not summary.ok:
                    raise RuntimeError("bundle loader failed")
                completed += len(changed)
            except WarehouseConnectionError:
                raise
            except AcquisitionDeferred:
                deferred += len(changed)
            except Exception as error:
                failed += len(changed)
                LOGGER.error(
                    "fitbit bundle failed bundle_id=%s error_type=%s",
                    bundle.bundle_id,
                    type(error).__name__,
                )
        state.retire_superseded_bundles()
        return AcquisitionSummary(completed, failed, deferred)
