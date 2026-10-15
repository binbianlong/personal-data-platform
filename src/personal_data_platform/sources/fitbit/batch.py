"""Bounded pull collection and shared-session Fitbit notification acquisition."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from google.api_core.exceptions import DeadlineExceeded

from personal_data_platform.loader.deadline import interrupt_after
from personal_data_platform.storage.motherduck import Warehouse

from .acquisition import AcquisitionRunner, acquisition_windows
from .models import DATE_TYPES, Notification
from .notifications import decode_notification

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QueuedNotification:
    notification: Notification
    ack_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class AcquisitionUnit:
    notification: Notification
    originals: tuple[QueuedNotification, ...]


def group_notifications(
    messages: list[QueuedNotification], *, now: datetime
) -> tuple[list[AcquisitionUnit], list[QueuedNotification]]:
    """Merge complete bounded scopes only inside one acquisition day."""
    buckets: dict[tuple[object, ...], list[QueuedNotification]] = {}
    deferred = []
    for message in messages:
        notice = message.notification
        window = notice.windows[0]
        if notice.acquisition_mode == "bounded_minutes" and window.end > now.replace(
            second=0, microsecond=0
        ):
            deferred.append(message)
            continue
        zone = UTC if window.time_basis == "civil" else ZoneInfo("Asia/Tokyo")
        day = window.start.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        key: tuple[object, ...] = (
            notice.subject_key,
            window.data_type,
            notice.acquisition_mode,
            window.time_basis,
        )
        if notice.acquisition_mode == "legacy" or window.data_type in DATE_TYPES:
            key += (window,)
        elif window.end > day + timedelta(days=1):
            # A noncanonical scope retains its original acquisition and ACK dependency.
            key += (message.ack_id,)
        else:
            key += (day,)
        buckets.setdefault(key, []).append(message)
    units = []
    for bucket in buckets.values():
        first = bucket[0].notification
        if first.acquisition_mode == "legacy":
            ranges = [(window, tuple(bucket)) for window in acquisition_windows(first.windows)]
        else:
            ranges = []
            for message in sorted(bucket, key=lambda m: m.notification.windows[0].start):
                window = message.notification.windows[0]
                if ranges and window.start <= ranges[-1][0].end:
                    previous, originals = ranges[-1]
                    ranges[-1] = (
                        replace(previous, end=max(previous.end, window.end)),
                        (*originals, message),
                    )
                else:
                    ranges.append((window, (message,)))
        for window, originals in ranges:
            notice = Notification(
                uuid.uuid4().hex,
                first.subject_key,
                (window,),
                min(m.notification.received_at for m in originals),
                first.acquisition_mode,
            )
            units.append(AcquisitionUnit(notice, originals))
    return units, deferred


class Message(Protocol):
    @property
    def data(self) -> bytes: ...
    @property
    def message_id(self) -> str: ...


class ReceivedMessage(Protocol):
    @property
    def ack_id(self) -> str: ...
    @property
    def message(self) -> Message: ...


class PullResponse(Protocol):
    @property
    def received_messages(self) -> Sequence[ReceivedMessage]: ...


class Subscriber(Protocol):
    def pull(self, *, request: dict[str, object], retry: None, timeout: float) -> PullResponse: ...
    def acknowledge(self, *, request: dict[str, object], retry: None, timeout: float) -> object: ...


@dataclass(frozen=True, slots=True)
class Collection:
    messages: list[QueuedNotification]
    received: int = 0
    rejected: int = 0


def collect_notifications(
    subscriber: Subscriber,
    subscription: str,
    *,
    subject_key: str,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> Collection:
    messages = []
    received = rejected = size = 0
    while received < 1000 and size < 8 * 1024 * 1024:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            response = subscriber.pull(
                request={"subscription": subscription, "max_messages": min(100, 1000 - received)},
                retry=None,
                timeout=min(2.0, remaining),
            )
        except DeadlineExceeded:
            break
        if not response.received_messages:
            break
        overflow = False
        for message in response.received_messages:
            received += 1
            size += len(message.message.data)
            if received > 1000 or size > 8 * 1024 * 1024:
                rejected += 1
                overflow = True
                continue
            try:
                notice = decode_notification(message.message.data)
                if notice.subject_key != subject_key or not message.ack_id:
                    raise ValueError("unexpected notification subject or missing acknowledgment")
                messages.append(
                    QueuedNotification(notice, message.ack_id, message.message.message_id)
                )
            except (ValueError, KeyError, TypeError):
                rejected += 1
                LOGGER.error(
                    "fitbit invalid pull notification", extra={"event": "fitbit_batch_rejected"}
                )
        if overflow:
            LOGGER.error(
                "fitbit pull collection limit exceeded", extra={"event": "fitbit_batch_rejected"}
            )
            break
    return Collection(messages, received, rejected)


@dataclass(slots=True)
class BatchSummary:
    batch_id: str
    received: int = 0
    grouped: int = 0
    completed: int = 0
    failed: int = 0
    deferred: int = 0
    acked: int = 0
    session_opens: int = 0
    elapsed_seconds: float = 0

    @property
    def ok(self) -> bool:
        return self.failed == self.deferred == 0


class NotificationDrainer:
    def __init__(
        self, *, runner: AcquisitionRunner, subscriber: Subscriber, subscription: str
    ) -> None:
        self.runner, self.subscriber, self.subscription = runner, subscriber, subscription

    def run(self) -> BatchSummary:
        """Commit independent groups in one leased session, then ACK their originals."""
        started = self.runner.monotonic()
        deadline = started + 480
        work_deadline = deadline - 20
        summary = BatchSummary(uuid.uuid4().hex)
        messages: list[QueuedNotification] = []
        units: list[AcquisitionUnit] = []
        pending: dict[str, int] = {}
        groups: dict[str, list[str]] = {}
        outcomes: dict[str, str] = {}
        acked: set[str] = set()
        committed_at: dict[str, datetime] = {}
        acknowledged_at: dict[str, datetime] = {}
        started_at = self.runner.clock()
        warehouse: Warehouse | None = None
        timer = None
        owner = uuid.uuid4().hex
        acquired = False
        try:
            if self.runner.paused:
                summary.deferred = 1
                return summary
            collected = collect_notifications(
                self.subscriber,
                self.subscription,
                subject_key=self.runner.subject_key,
                deadline=min(started + 10, work_deadline),
                monotonic=self.runner.monotonic,
            )
            messages = collected.messages
            summary.received, summary.failed = collected.received, collected.rejected
            units, incomplete = group_notifications(messages, now=self.runner.clock())
            summary.grouped = len(units)
            summary.deferred = len(incomplete)
            outcomes.update((message.ack_id, "deferred") for message in incomplete)
            for unit in units:
                for message in unit.originals:
                    pending[message.ack_id] = pending.get(message.ack_id, 0) + 1
                    groups.setdefault(message.ack_id, []).append(unit.notification.notification_id)
            if not units:
                return summary
            warehouse = self.runner.warehouse_factory()
            summary.session_opens = 1
            # Connection creation cannot be interrupted. Check its entire elapsed time on return.
            if self.runner.monotonic() >= work_deadline:
                summary.deferred += len(units)
                return summary
            timer = interrupt_after(warehouse, work_deadline - self.runner.monotonic())
            warehouse.migrate()
            if self.runner.monotonic() >= work_deadline:
                summary.deferred += len(units)
                return summary
            acquired = warehouse.acquire_job_lock("loader", owner, lease_seconds=600)
            if not acquired:
                summary.deferred += len(units)
                return summary
            for index, unit in enumerate(units):
                remaining = int(work_deadline - self.runner.monotonic())
                if remaining <= 0 or not warehouse.connection_usable:
                    summary.deferred += len(units) - index
                    break
                result = self.runner.process_notification(
                    unit.notification,
                    attempt_id=summary.batch_id,
                    warehouse=warehouse,
                    lease_owner=owner,
                    timeout_seconds=remaining,
                )
                if result.ok:
                    summary.completed += 1
                    for message in unit.originals:
                        pending[message.ack_id] -= 1
                        if pending[message.ack_id] == 0:
                            committed_at[message.ack_id] = self.runner.clock()
                else:
                    if result.failed_scopes:
                        summary.failed += 1
                    else:
                        summary.deferred += 1
                    for message in unit.originals:
                        outcomes[message.ack_id] = "failed" if result.failed_scopes else "deferred"
                ready = [
                    message.ack_id
                    for message in unit.originals
                    if pending[message.ack_id] == 0
                    and message.ack_id not in outcomes
                    and message.ack_id not in acked
                ]
                if ready:
                    remaining_ack = deadline - self.runner.monotonic()
                    if remaining_ack <= 0:
                        summary.failed += 1
                        break
                    try:
                        self.subscriber.acknowledge(
                            request={"subscription": self.subscription, "ack_ids": ready},
                            retry=None,
                            timeout=min(2.0, remaining_ack),
                        )
                    except Exception as error:
                        summary.failed += 1
                        outcomes.update((ack_id, "failed") for ack_id in ready)
                        LOGGER.error(
                            "fitbit batch acknowledgment unknown error_type=%s",
                            type(error).__name__,
                        )
                    else:
                        acked.update(ready)
                        acknowledged_at.update((ack_id, self.runner.clock()) for ack_id in ready)
                        summary.acked += len(ready)
        except Exception as error:
            summary.failed += 1
            LOGGER.error("fitbit batch failed error_type=%s", type(error).__name__)
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if warehouse is not None:
                try:
                    if acquired and warehouse.connection_usable:
                        warehouse.release_job_lock("loader", owner)
                except Exception as error:
                    summary.failed += 1
                    LOGGER.error(
                        "fitbit batch lease cleanup failed error_type=%s", type(error).__name__
                    )
                finally:
                    try:
                        warehouse.close()
                    except Exception as error:
                        summary.failed += 1
                        LOGGER.error(
                            "fitbit batch close failed error_type=%s", type(error).__name__
                        )
            finished = self.runner.clock()
            for message in messages:
                notice = message.notification
                group_ids = groups.get(message.ack_id, [])
                LOGGER.info(
                    "fitbit batch original completed",
                    extra={
                        "event": "fitbit_push",
                        "status": "succeeded" if message.ack_id in acked else "retry",
                        "summary": {
                            "notification_id": notice.notification_id,
                            "pubsub_message_id": message.message_id,
                            "attempt_id": summary.batch_id,
                            "batch_id": summary.batch_id,
                            "group_id": group_ids[0] if len(group_ids) == 1 else None,
                            "group_ids": group_ids,
                            "acquisition_mode": notice.acquisition_mode,
                            "received_at": notice.received_at.isoformat(),
                            "started_at": started_at.isoformat(),
                            "finished_at": acknowledged_at.get(
                                message.ack_id, finished
                            ).isoformat(),
                            "committed_at": committed_at[message.ack_id].isoformat()
                            if message.ack_id in committed_at
                            else None,
                            "lag_seconds": (
                                committed_at[message.ack_id] - notice.received_at
                            ).total_seconds()
                            if message.ack_id in committed_at
                            else None,
                            "completed_scopes": len(group_ids) - pending.get(message.ack_id, 0),
                            "failed_scopes": int(outcomes.get(message.ack_id) == "failed"),
                            "deferred_scopes": int(
                                message.ack_id not in acked
                                and outcomes.get(message.ack_id) != "failed"
                            ),
                        },
                    },
                )
            summary.elapsed_seconds = round(self.runner.monotonic() - started, 3)
            LOGGER.info(
                "fitbit batch finished",
                extra={
                    "event": "fitbit_batch",
                    "status": "succeeded" if summary.ok else "retry",
                    "summary": asdict(summary),
                },
            )
        return summary
