"""Compose the west receiver, notification job and explicit range acquisition."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import uvicorn

from personal_data_platform.config import schema_profile, secret_config
from personal_data_platform.loader.job import JobAlreadyRunning
from personal_data_platform.storage.gcs import GCSRawRepository
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

from .adapter import FitbitSource
from .api import HealthClient
from .logging import configure_logging
from .models import DATA_TYPES, DATE_TYPES, Window, date_cursor
from .oauth import GoogleOAuth
from .service import create_pubsub_app
from .signatures import TinkSignatures
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


def _warehouse() -> Warehouse:
    return Warehouse(connect(WarehouseConfig.from_env()))


def delivery_mode() -> Literal["pubsub"]:
    schema_profile()
    if os.environ.get("PDP_FITBIT_DELIVERY_MODE", "pubsub") != "pubsub":
        raise ValueError("PDP_FITBIT_DELIVERY_MODE must be pubsub")
    return "pubsub"


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
    delivery_mode()
    return _acquisition_runner().ingest(
        PubSubNotifications.from_env(),
        max_messages=max_messages,
        collect_seconds=collect_seconds,
        timeout_seconds=timeout_seconds,
    )


def run_serve_from_env() -> int:
    from .notifications import PubSubNotifications

    configure_logging()
    delivery_mode()
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


def _tokyo_start(day: date) -> datetime:
    return datetime.combine(day, time(), ZoneInfo("Asia/Tokyo"))
