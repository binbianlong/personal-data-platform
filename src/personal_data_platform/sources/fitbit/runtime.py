"""Environment composition for opt-in service, synchronization and scheduled repair."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

import google.cloud.storage as storage
import uvicorn
from google.cloud import tasks_v2

from personal_data_platform.config import GCSConfig, schema_profile, secret_config
from personal_data_platform.loader.job import JobAlreadyRunning
from personal_data_platform.reconciliation.job import (
    RECONCILIATION_LEASE_SECONDS,
    run_reconciliation,
)
from personal_data_platform.storage.gcs import GCSRawRepository
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

from .adapter import FitbitSource
from .api import HealthClient, HealthError, SyncTime
from .logging import configure_logging
from .models import DATA_TYPES, DATE_TYPES, Window, date_cursor
from .oauth import GoogleOAuth
from .receipts import (
    RECEIPT_PREFIX,
    GCSReceiptRepository,
    Receipt,
    ReceiptReadConflict,
    ReceiptRepository,
    validate_receipt_key,
)
from .service import Queue, ReceiptWorker, create_app, create_pubsub_app
from .signatures import GoogleTaskIdentity, TinkSignatures
from .sync_state import GCSFitbitSyncState, StoredSyncState
from .webhook import GoogleHealthAuthenticator

if TYPE_CHECKING:
    from .acquisition import AcquisitionRunner, AcquisitionSummary

LOGGER = logging.getLogger(__name__)


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def enabled(name: str) -> bool:
    value = os.environ.get(name, "false").lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value == "true"


class CloudTasksQueue:
    def __init__(self, *, parent: str, service_url: str, service_account: str) -> None:
        if not service_url.startswith("https://"):
            raise ValueError("task endpoint must be HTTPS")
        self._client = tasks_v2.CloudTasksClient()
        self._parent, self._url, self._account = parent, service_url.rstrip("/"), service_account

    def enqueue(self, receipt_key: str) -> None:
        validate_receipt_key(receipt_key)
        # No permanent content-hash task name: a later A -> B -> A must run.
        self._client.create_task(
            request={
                "parent": self._parent,
                "task": {
                    "http_request": {
                        "http_method": tasks_v2.HttpMethod.POST,
                        "url": self._url + "/internal/tasks/fitbit",
                        "headers": {"Content-Type": "application/json"},
                        "body": json.dumps({"receipt_key": receipt_key}).encode(),
                        "oidc_token": {
                            "service_account_email": self._account,
                            "audience": self._url,
                        },
                    },
                    "dispatch_deadline": {"seconds": 1800},
                },
            },
            # Cloud Tasks caps RPC deadlines at 30s. Leave room for clock skew
            # and transport overhead instead of sending the exact upper bound.
            timeout=20,
        )


def _queue() -> CloudTasksQueue:
    return CloudTasksQueue(
        parent=required("PDP_FITBIT_TASKS_PARENT"),
        service_url=required("PDP_FITBIT_SERVICE_URL"),
        service_account=required("PDP_FITBIT_TASK_SERVICE_ACCOUNT"),
    )


def _stores() -> tuple[GCSReceiptRepository, GCSRawRepository]:
    config = GCSConfig.from_env()
    client = storage.Client(project=config.project_id)
    return (
        GCSReceiptRepository(client=client, bucket=config.bucket),
        GCSRawRepository(client=client, bucket=config.bucket, source=FitbitSource()),
    )


def _warehouse() -> Warehouse:
    return Warehouse(connect(WarehouseConfig.from_env()))


def _worker(receipts: GCSReceiptRepository, repository: GCSRawRepository) -> ReceiptWorker:
    return ReceiptWorker(
        receipts=receipts,
        repository=repository,
        client=HealthClient(access_token=GoogleOAuth.from_env()),
        warehouse_factory=_warehouse,
        subject_key=required("PDP_FITBIT_SUBJECT_KEY"),
        paused=enabled("PDP_FITBIT_PROCESSING_PAUSED"),
    )


def delivery_mode() -> Literal["legacy", "pubsub"]:
    mode = os.environ.get(
        "PDP_FITBIT_DELIVERY_MODE", "pubsub" if schema_profile() == "west" else "legacy"
    )
    if mode not in ("legacy", "pubsub"):
        raise ValueError("PDP_FITBIT_DELIVERY_MODE must be legacy or pubsub")
    return "pubsub" if mode == "pubsub" else "legacy"


def _acquisition_runner() -> AcquisitionRunner:
    from .acquisition import AcquisitionRunner

    values = dict(os.environ)
    values["PDP_FITBIT_DELIVERY_MODE"] = "pubsub"
    oauth = GoogleOAuth.from_env(values)
    return AcquisitionRunner(
        repository=GCSRawRepository.from_env(source=FitbitSource(version=3)),
        client=HealthClient(access_token=oauth),
        warehouse_factory=_warehouse,
        subject_key=required("PDP_FITBIT_SUBJECT_KEY"),
        paused=enabled("PDP_FITBIT_PROCESSING_PAUSED"),
    )


def run_notification_job(
    *, max_messages: int = 500, collect_seconds: int = 120, timeout_seconds: int = 3000
) -> AcquisitionSummary:
    from .notifications import PubSubNotifications

    configure_logging()
    if delivery_mode() != "pubsub":
        raise ValueError("ingest-notifications requires pubsub delivery mode")
    return _acquisition_runner().ingest(
        PubSubNotifications.from_env(),
        max_messages=max_messages,
        collect_seconds=collect_seconds,
        timeout_seconds=timeout_seconds,
    )


def run_serve_from_env() -> int:
    configure_logging()
    if delivery_mode() == "pubsub":
        from .notifications import PubSubNotifications

        if os.environ.get("PDP_FITBIT_WEBHOOK_AUTHORIZATION") or os.environ.get(
            "PDP_FITBIT_HEALTH_USER_ID"
        ):
            raise ValueError("legacy webhook settings conflict with pubsub configuration")
        config = secret_config("PDP_FITBIT_WEBHOOK_CONFIG", ("authorization", "health_user_id"))
        app = create_pubsub_app(
            authenticator=GoogleHealthAuthenticator(
                authorization=config["authorization"],
                health_user_id=config["health_user_id"],
                subject_key=required("PDP_FITBIT_SUBJECT_KEY"),
                signatures=TinkSignatures(),
            ),
            notifications=PubSubNotifications.from_env(),
        )
    else:
        receipts, repository = _stores()
        app = create_app(
            authenticator=GoogleHealthAuthenticator(
                authorization=required("PDP_FITBIT_WEBHOOK_AUTHORIZATION"),
                health_user_id=required("PDP_FITBIT_HEALTH_USER_ID"),
                subject_key=required("PDP_FITBIT_SUBJECT_KEY"),
                signatures=TinkSignatures(),
            ),
            identity=GoogleTaskIdentity(
                audience=required("PDP_FITBIT_SERVICE_URL").rstrip("/"),
                service_account=required("PDP_FITBIT_TASK_SERVICE_ACCOUNT"),
            ),
            receipts=receipts,
            queue=_queue(),
            worker=_worker(receipts, repository),
        )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
        access_log=False,
    )
    return 0


def sync_windows(start: datetime, end: datetime, data_types: tuple[str, ...]) -> tuple[Window, ...]:
    windows = []
    for kind in data_types:
        if kind in DATE_TYPES:
            first = start.astimezone(ZoneInfo("Asia/Tokyo")).date()
            last = end.astimezone(ZoneInfo("Asia/Tokyo"))
            stop = date_cursor(last.date())
            if last.time().isoformat() != "00:00:00":
                stop += timedelta(days=1)
            windows.append(Window(kind, date_cursor(first), stop))
        else:
            windows.append(Window(kind, start, end))
    return tuple(windows)


def run_sync_from_env(
    *, start: datetime, end: datetime, data_types: tuple[str, ...] = DATA_TYPES
) -> int:
    """Acquire an explicit range; progress lives in completed Raw and coverage."""
    from personal_data_platform.loader.job import LOADER_LEASE_SECONDS

    configure_logging()
    if start.tzinfo is None or end.tzinfo is None or start >= end or not data_types:
        raise ValueError("sync needs an increasing timezone-aware range and data types")
    warehouse = _warehouse()
    owner = str(uuid4())
    acquired = False
    job_started = False
    details: dict[str, object] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "data_types": list(data_types),
    }
    try:
        warehouse.migrate(profile=schema_profile())
        acquired = warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS)
        if not acquired:
            raise JobAlreadyRunning("loader already has an unexpired job lease")
        warehouse.begin_job("fitbit-sync", owner)
        job_started = True
        result = _acquisition_runner().run_windows(
            sync_windows(start, end, data_types),
            warehouse=warehouse,
            lease_owner=owner,
            timeout_seconds=50 * 60,
        )
        payload = json.loads(json.dumps(asdict(result), default=str))
        warehouse.finish_job(owner, succeeded=result.ok, details={**details, **payload})
        job_started = False
        print(json.dumps(payload, sort_keys=True))
        return int(not result.ok)
    finally:
        if job_started and warehouse.connection_usable:
            warehouse.finish_job(owner, succeeded=False, details=details)
        if acquired and warehouse.connection_usable:
            warehouse.release_job_lock("loader", owner)
        warehouse.close()


def run_daily_repair(
    *, now: datetime, warehouse: Warehouse, lease_owner: str, timeout_seconds: int
) -> AcquisitionSummary:
    """Verify exactly seven completed days; larger gaps need explicit sync."""
    from .acquisition import AcquisitionSummary

    configure_logging()
    if now.tzinfo is None:
        raise ValueError("daily repair time must be timezone-aware")
    if enabled("PDP_FITBIT_PROCESSING_PAUSED"):
        return AcquisitionSummary(deferred_scopes=1)
    stop = _tokyo_start(now.astimezone(ZoneInfo("Asia/Tokyo")).date())
    return _acquisition_runner().run_windows(
        sync_windows(stop - timedelta(days=7), stop, DATA_TYPES),
        warehouse=warehouse,
        lease_owner=lease_owner,
        timeout_seconds=timeout_seconds,
    )


RepairPhase = Literal["scheduled_receipts", "receipt_inventory", "raw_audit", "orphan_recovery"]
RepairStatus = Literal["disabled", "paused", "succeeded", "deferred", "failed"]


@dataclass(frozen=True, slots=True)
class RepairSummary:
    enabled: bool = True
    pending_count: int = 0
    queued_count: int = 0
    failed_count: int = 0
    oldest_pending_seconds: float = 0
    at_risk_count: int = 0
    latest_received_at: str | None = None
    latest_completed_at: str | None = None
    paused: bool = False
    deferred_phases: tuple[RepairPhase, ...] = ()
    receipt_read_deferred_count: int = 0
    status: RepairStatus = field(init=False)

    def __post_init__(self) -> None:
        status: RepairStatus
        if self.failed_count or self.at_risk_count:
            status = "failed"
        elif not self.enabled:
            status = "disabled"
        elif self.paused:
            status = "paused"
        elif self.deferred_phases or self.receipt_read_deferred_count:
            status = "deferred"
        else:
            status = "succeeded"
        object.__setattr__(self, "status", status)


class DeviceClient(Protocol):
    def latest_tracker_sync(self) -> SyncTime | None: ...


class DeviceCheckError(RuntimeError):
    """A tracker lookup that should be reported without hiding storage failures."""


def _tokyo_start(day: date) -> datetime:
    return datetime.combine(day, time(), ZoneInfo("Asia/Tokyo"))


def _week(day: date) -> str:
    year, number, _ = day.isocalendar()
    return f"{year:04d}-W{number:02d}"


def _receipt_key(day: date, identity: str) -> str:
    return f"{RECEIPT_PREFIX}{day.isoformat()}/{identity}.json"


def _ensure_scheduled(
    receipts: ReceiptRepository,
    *,
    subject_key: str,
    key: str,
    windows: tuple[Window, ...],
    received_at: datetime,
    origin: str,
) -> None:
    expected = Receipt.create(subject_key, windows, received_at=received_at, key=key, origin=origin)
    current = receipts.create(expected).receipt

    def matches(actual: Window, original: Window) -> bool:
        if actual.data_type != original.data_type:
            return False
        if actual.data_type in ("steps", "active-zone-minutes"):
            return actual.start <= original.start and actual.end >= original.end
        return actual == original

    if (
        current.subject_key != subject_key
        or current.origin != origin
        or len(current.work) != len(expected.work)
        or any(
            not matches(actual.window, original.window)
            for actual, original in zip(current.work, expected.work, strict=True)
        )
    ):
        raise RuntimeError("scheduled Fitbit receipt identity changed")


def _completed(receipts: ReceiptRepository, key: str) -> bool:
    try:
        return receipts.read(key).receipt.completed_at is not None
    except FileNotFoundError:
        return False


def _bootstrap(
    receipts: ReceiptRepository,
    sync_store: GCSFitbitSyncState,
    stored: StoredSyncState,
    *,
    subject_key: str,
    today: date,
    now: datetime,
) -> StoredSyncState:
    state = stored.state
    if state.bootstrap_day is None:
        state = replace(state, bootstrap_day=today)
        stored = sync_store.replace(stored, state)
    assert state.bootstrap_day is not None
    day = state.bootstrap_day
    key = _receipt_key(day, "bootstrap")
    _ensure_scheduled(
        receipts,
        subject_key=subject_key,
        key=key,
        windows=sync_windows(_tokyo_start(day - timedelta(days=7)), _tokyo_start(day), DATA_TYPES),
        received_at=now,
        origin="bootstrap",
    )
    if _completed(receipts, key):
        state = replace(
            state,
            bootstrap_complete=True,
            last_completed_sync=SyncTime.from_datetime(_tokyo_start(day)),
            weekly_completed=_week(day),
        )
        stored = sync_store.replace(stored, state)
    return stored


def _weekly(
    receipts: ReceiptRepository,
    sync_store: GCSFitbitSyncState,
    stored: StoredSyncState,
    *,
    subject_key: str,
    today: date,
    now: datetime,
) -> StoredSyncState:
    state = stored.state
    current_week = _week(today)
    if state.weekly_pending is None and state.weekly_completed != current_week:
        state = replace(
            state, weekly_pending=current_week, weekly_end_day=today - timedelta(days=1)
        )
        stored = sync_store.replace(stored, state)
    if state.weekly_pending is None:
        return stored
    assert state.weekly_end_day is not None
    monday = state.weekly_end_day + timedelta(days=1)
    monday -= timedelta(days=monday.weekday())
    key = _receipt_key(monday, "weekly")
    end = state.weekly_end_day + timedelta(days=1)
    _ensure_scheduled(
        receipts,
        subject_key=subject_key,
        key=key,
        windows=sync_windows(_tokyo_start(end - timedelta(days=7)), _tokyo_start(end), DATA_TYPES),
        received_at=now,
        origin="weekly",
    )
    if _completed(receipts, key):
        stored = sync_store.replace(
            stored,
            replace(
                state,
                weekly_completed=state.weekly_pending,
                weekly_pending=None,
                weekly_end_day=None,
            ),
        )
    return stored


def _device_key(subject_key: str, target: SyncTime, day: date) -> str:
    identity = hashlib.sha256(
        f"{subject_key}:{target.text}:{day.isoformat()}".encode()
    ).hexdigest()[:12]
    return _receipt_key(day, f"device-{identity}")


def _device_windows(day: date, target: SyncTime) -> tuple[Window, ...]:
    start = _tokyo_start(day)
    end = _tokyo_start(day + timedelta(days=1))
    if day == target.tokyo_date():
        # Reconcile's upper bound is exclusive; include the sync instant.
        end = min(end, target.utc_second + timedelta(microseconds=target.nanosecond // 1000 + 1))
    if end <= start:
        raise ValueError("tracker sync time does not cover the selected day")
    return sync_windows(start, end, DATA_TYPES)


def _device(
    receipts: ReceiptRepository,
    sync_store: GCSFitbitSyncState,
    stored: StoredSyncState,
    device_client: DeviceClient,
    *,
    subject_key: str,
    today: date,
    now: datetime,
) -> StoredSyncState:
    state = stored.state
    if state.device_target is None:
        try:
            latest = device_client.latest_tracker_sync()
        except HealthError as error:
            raise DeviceCheckError("paired tracker request failed") from error
        if latest is None:
            raise DeviceCheckError("no paired tracker sync time is available")
        if latest > SyncTime.from_datetime(now + timedelta(minutes=5)):
            raise DeviceCheckError("tracker sync time is in the future")
        assert state.last_completed_sync is not None
        if latest <= state.last_completed_sync:
            return stored
        state = replace(
            state,
            device_target=latest,
            device_next_day=state.last_completed_sync.tokyo_date(),
        )
        stored = sync_store.replace(stored, state)
    assert state.device_target is not None and state.device_next_day is not None
    if state.device_batch_end is None:
        end_day = min(state.device_next_day + timedelta(days=89), state.device_target.tokyo_date())
        state = replace(state, device_batch_end=end_day)
        stored = sync_store.replace(stored, state)
    assert state.device_batch_end is not None
    next_day = state.device_next_day
    batch_end = state.device_batch_end
    target = state.device_target
    assert next_day is not None and batch_end is not None and target is not None
    days = [next_day + timedelta(days=index) for index in range((batch_end - next_day).days + 1)]
    for day in days:
        _ensure_scheduled(
            receipts,
            subject_key=subject_key,
            key=_device_key(subject_key, target, day),
            windows=_device_windows(day, target),
            received_at=now,
            origin="device-sync",
        )
    if all(_completed(receipts, _device_key(subject_key, target, day)) for day in days):
        next_day = batch_end + timedelta(days=1)
        if next_day > target.tokyo_date():
            state = replace(
                state,
                last_completed_sync=target,
                device_target=None,
                device_next_day=None,
                device_batch_end=None,
            )
        else:
            state = replace(state, device_next_day=next_day, device_batch_end=None)
        stored = sync_store.replace(stored, state)
    return stored


def repair_receipts(
    receipts: ReceiptRepository,
    queue: Queue,
    *,
    subject_key: str,
    now: datetime,
    paused: bool = False,
    sync_store: GCSFitbitSyncState,
    device_client: DeviceClient,
) -> RepairSummary:
    today = now.astimezone(ZoneInfo("Asia/Tokyo")).date()
    failed = 0
    deferred: list[RepairPhase] = []
    if not paused:
        try:
            stored = sync_store.read(subject_key)
            if not stored.state.bootstrap_complete:
                stored = _bootstrap(
                    receipts,
                    sync_store,
                    stored,
                    subject_key=subject_key,
                    today=today,
                    now=now,
                )
            if stored.state.bootstrap_complete:
                stored = _weekly(
                    receipts,
                    sync_store,
                    stored,
                    subject_key=subject_key,
                    today=today,
                    now=now,
                )
                try:
                    _device(
                        receipts,
                        sync_store,
                        stored,
                        device_client,
                        subject_key=subject_key,
                        today=today,
                        now=now,
                    )
                except DeviceCheckError as error:
                    LOGGER.error("fitbit paired-device check failed: %s", type(error).__name__)
                    failed += 1
        except ReceiptReadConflict:
            deferred.append("scheduled_receipts")
            LOGGER.info("fitbit repair deferred phase=scheduled_receipts")
    inventory = receipts.inventory()
    if inventory.deferred_count:
        deferred.append("receipt_inventory")
    queued = at_risk = 0
    oldest = 0.0
    for pending_receipt in inventory.pending:
        age = max(0.0, (now - pending_receipt.receipt.received_at).total_seconds())
        oldest = max(oldest, age)
        at_risk += age >= 87 * 86400
        if paused:
            continue
        try:
            if pending_receipt.receipt.subject_key != subject_key:
                raise ValueError("receipt subject does not match runtime")
            queue.enqueue(pending_receipt.receipt.key)
            queued += 1
        except Exception as error:
            LOGGER.error("fitbit repair enqueue failed: %s", type(error).__name__)
            failed += 1
    return RepairSummary(
        paused=paused,
        deferred_phases=tuple(deferred),
        receipt_read_deferred_count=inventory.deferred_count,
        pending_count=len(inventory.pending),
        queued_count=queued,
        failed_count=failed,
        oldest_pending_seconds=oldest,
        at_risk_count=at_risk,
        latest_received_at=inventory.latest_received_at.isoformat()
        if inventory.latest_received_at
        else None,
        latest_completed_at=inventory.latest_completed_at.isoformat()
        if inventory.latest_completed_at
        else None,
    )


def _report_repair(summary: RepairSummary) -> RepairSummary:
    details = asdict(summary)
    LOGGER.info(
        "fitbit repair %s",
        json.dumps(details),
        extra={"event": "fitbit_repair", "status": summary.status, "summary": details},
    )
    if summary.failed_count or summary.at_risk_count:
        LOGGER.error("fitbit repair backlog requires attention")
    return summary


def _defer_repair(summary: RepairSummary, phase: RepairPhase) -> RepairSummary:
    LOGGER.info("fitbit repair deferred phase=%s", phase)
    return replace(summary, deferred_phases=(*summary.deferred_phases, phase))


def run_repair_from_env() -> RepairSummary:
    configure_logging()
    try:
        from personal_data_platform.config import schema_profile

        if schema_profile() == "west" or os.environ.get("PDP_FITBIT_DELIVERY_MODE") == "pubsub":
            from personal_data_platform.loader.job import LOADER_LEASE_SECONDS

            warehouse = _warehouse()
            owner = str(uuid4())
            acquired = False
            try:
                warehouse.migrate(profile=schema_profile())
                acquired = warehouse.acquire_job_lock(
                    "loader", owner, lease_seconds=LOADER_LEASE_SECONDS
                )
                if not acquired:
                    return _report_repair(RepairSummary(deferred_phases=("raw_audit",)))
                result = run_daily_repair(
                    now=datetime.now(UTC),
                    warehouse=warehouse,
                    lease_owner=owner,
                    timeout_seconds=100 * 60,
                )
                return _report_repair(
                    RepairSummary(
                        failed_count=result.failed_scopes,
                        deferred_phases=("raw_audit",) if result.deferred_scopes else (),
                    )
                )
            finally:
                if acquired and warehouse.connection_usable:
                    warehouse.release_job_lock("loader", owner)
                warehouse.close()
        return _run_repair_from_env()
    except Exception as error:
        LOGGER.error(
            "fitbit repair failed: %s",
            type(error).__name__,
            extra={"event": "fitbit_repair", "status": "failed"},
        )
        raise


def _run_repair_from_env() -> RepairSummary:
    if not enabled("PDP_FITBIT_REPAIR_ENABLED"):
        return _report_repair(RepairSummary(enabled=False))
    receipts, repository = _stores()
    paused = enabled("PDP_FITBIT_PROCESSING_PAUSED")
    config = GCSConfig.from_env()
    sync_store = GCSFitbitSyncState(
        client=storage.Client(project=config.project_id), bucket=config.bucket
    )
    device_client = HealthClient(access_token=GoogleOAuth.from_env())
    summary = repair_receipts(
        receipts,
        _queue(),
        subject_key=required("PDP_FITBIT_SUBJECT_KEY"),
        now=datetime.now(UTC),
        paused=paused,
        sync_store=sync_store,
        device_client=device_client,
    )
    if not paused:
        warehouse = _warehouse()
        owner = str(uuid4())
        acquired = False
        try:
            acquired = warehouse.acquire_job_lock(
                "reconciliation", owner, lease_seconds=RECONCILIATION_LEASE_SECONDS
            )
            if not acquired:
                summary = _defer_repair(summary, "raw_audit")
            else:
                try:
                    audited = run_reconciliation(
                        repository, warehouse, source=FitbitSource(), heartbeat=lambda _: None
                    )
                    if not audited.ok:
                        summary = replace(summary, failed_count=summary.failed_count + 1)
                except JobAlreadyRunning:
                    summary = _defer_repair(summary, "raw_audit")
        finally:
            if acquired and warehouse.connection_usable:
                warehouse.release_job_lock("reconciliation", owner)
            warehouse.close()
        try:
            _worker(receipts, repository).recover_orphan_intents(limit=1)
        except (JobAlreadyRunning, ReceiptReadConflict):
            summary = _defer_repair(summary, "orphan_recovery")
    return _report_repair(summary)
