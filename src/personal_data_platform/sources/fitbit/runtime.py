"""Environment composition for opt-in service, synchronization and scheduled repair."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import google.cloud.storage as storage
import uvicorn
from google.cloud import tasks_v2

from personal_data_platform.config import GCSConfig
from personal_data_platform.reconciliation.job import (
    RECONCILIATION_LEASE_SECONDS,
    run_reconciliation,
)
from personal_data_platform.storage.gcs import GCSRawRepository
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

from .adapter import FitbitSource
from .api import HealthClient
from .models import DATA_TYPES, DATE_TYPES, Window, date_cursor
from .oauth import GoogleOAuth
from .receipts import (
    RECEIPT_PREFIX,
    GCSReceiptRepository,
    Receipt,
    ReceiptRepository,
    validate_receipt_key,
)
from .service import Queue, ReceiptWorker, create_app
from .signatures import GoogleTaskIdentity, TinkSignatures
from .webhook import GoogleHealthAuthenticator

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
            timeout=30,
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


def run_serve_from_env() -> int:
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
    receipts, repository = _stores()
    receipt = Receipt.create(
        required("PDP_FITBIT_SUBJECT_KEY"),
        sync_windows(start, end, data_types),
        received_at=datetime.now(UTC),
        origin="manual",
    )
    stored = receipts.create(receipt)
    worker = _worker(receipts, repository)
    while not worker.run(stored.receipt.key):
        pass
    return 0


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


def repair_receipts(
    receipts: ReceiptRepository,
    queue: Queue,
    *,
    subject_key: str,
    now: datetime,
    paused: bool = False,
) -> RepairSummary:
    today = now.astimezone(ZoneInfo("Asia/Tokyo")).date()
    start = datetime.combine(today - timedelta(days=6), datetime.min.time(), ZoneInfo("Asia/Tokyo"))
    end = start + timedelta(days=7)
    daily = Receipt.create(
        subject_key, sync_windows(start, end, DATA_TYPES), received_at=now, daily=True
    )
    receipts.create(replace(daily, key=f"{RECEIPT_PREFIX}{today.isoformat()}/daily.json"))
    inventory = receipts.inventory()
    queued = failed = at_risk = 0
    oldest = 0.0
    for stored in inventory.pending:
        age = max(0.0, (now - stored.receipt.received_at).total_seconds())
        oldest = max(oldest, age)
        at_risk += age >= 27 * 86400
        if paused:
            continue
        try:
            if stored.receipt.subject_key != subject_key:
                raise ValueError("receipt subject does not match runtime")
            queue.enqueue(stored.receipt.key)
            queued += 1
        except Exception as error:
            LOGGER.error("fitbit repair enqueue failed: %s", type(error).__name__)
            failed += 1
    return RepairSummary(
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


def run_repair_from_env() -> RepairSummary:
    if not enabled("PDP_FITBIT_REPAIR_ENABLED"):
        return RepairSummary(enabled=False)
    receipts, repository = _stores()
    paused = enabled("PDP_FITBIT_PROCESSING_PAUSED")
    summary = repair_receipts(
        receipts,
        _queue(),
        subject_key=required("PDP_FITBIT_SUBJECT_KEY"),
        now=datetime.now(UTC),
        paused=paused,
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
                raise RuntimeError("reconciliation already has an unexpired job lease")
            audited = run_reconciliation(
                repository, warehouse, source=FitbitSource(), heartbeat=lambda _: None
            )
            if not audited.ok:
                summary = replace(summary, failed_count=summary.failed_count + 1)
        finally:
            if acquired and warehouse.connection_usable:
                warehouse.release_job_lock("reconciliation", owner)
            warehouse.close()
    LOGGER.info("fitbit repair %s", json.dumps(asdict(summary)))
    if summary.at_risk_count or summary.failed_count:
        LOGGER.error("fitbit repair backlog requires attention")
    return summary
