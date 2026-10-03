"""Compose daily Fitbit acquisition with the shared storage and warehouse."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from uuid import uuid4

from google.cloud import storage

from personal_data_platform.config import GCSConfig
from personal_data_platform.storage.gcs import GCSRawRepository
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

from .adapter import FitbitSource
from .api import HealthClient
from .daily import Client, DailySummary, Store, collect_daily, collect_range, sync_windows
from .logging import configure_logging
from .oauth import GoogleOAuth

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


def _repository() -> GCSRawRepository:
    config = GCSConfig.from_env()
    return GCSRawRepository(
        client=storage.Client(project=config.project_id),
        bucket=config.bucket,
        source=FitbitSource(),
    )


def _client() -> HealthClient:
    return HealthClient(access_token=GoogleOAuth.from_env())


def _warehouse() -> Warehouse:
    return Warehouse(connect(WarehouseConfig.from_env()))


def _record_progress(warehouse: Warehouse, summary: DailySummary) -> None:
    """Failures and incomplete runs preserve the last fully successful daily pass."""
    run_id = str(uuid4())
    warehouse.connection.execute(
        "INSERT INTO ops.heartbeat VALUES ('fitbit_daily_started', current_timestamp, ?, '{}') "
        "ON CONFLICT (monitor_name) DO NOTHING",
        [run_id],
    )
    if summary.status == "succeeded":
        warehouse.publish_heartbeat("fitbit_daily_pass", run_id, {"status": summary.status})
    age = warehouse.query_value(
        "SELECT epoch(current_timestamp - coalesce("
        "(SELECT succeeded_at FROM ops.heartbeat WHERE monitor_name='fitbit_daily_pass'),"
        "(SELECT succeeded_at FROM ops.heartbeat WHERE monitor_name='fitbit_daily_started')))"
    )
    summary.full_success_age_seconds = max(0.0, float(age))


def _report(summary: DailySummary, event: str) -> DailySummary:
    LOGGER.info(
        "%s %s",
        event,
        summary.status,
        extra={"event": event, "status": summary.status, "summary": asdict(summary)},
    )
    return summary


def _run(
    operation: Callable[[Store, Warehouse, Client, str], DailySummary],
    *,
    event: str,
    warehouse: Warehouse | None = None,
) -> DailySummary:
    configure_logging()
    if enabled("PDP_FITBIT_PROCESSING_PAUSED"):
        return _report(DailySummary(status="paused"), event)
    owned = warehouse is None
    current = warehouse or _warehouse()
    daily = event == "fitbit_daily"
    try:
        if owned:
            current.migrate()
        if daily:
            _record_progress(current, DailySummary(status="deferred"))
        try:
            result = operation(
                _repository(), current, _client(), required("PDP_FITBIT_SUBJECT_KEY")
            )
        except Exception as error:
            failed = DailySummary(status="failed")
            if daily and current.connection_usable:
                _record_progress(current, failed)
            LOGGER.error(
                "%s failed: %s",
                event,
                type(error).__name__,
                extra={"event": event, "status": "failed", "summary": asdict(failed)},
            )
            raise
        if daily:
            _record_progress(current, result)
        return _report(result, event)
    finally:
        if owned:
            current.close()


def run_daily_from_env(*, warehouse: Warehouse | None = None) -> DailySummary:
    return _run(
        lambda repository, current, client, subject: collect_daily(
            repository, current, client=client, subject_key=subject
        ),
        event="fitbit_daily",
        warehouse=warehouse,
    )


def run_sync_from_env(
    *, start: datetime, end: datetime, data_types: tuple[str, ...]
) -> DailySummary:
    windows = sync_windows(start, end, data_types)
    return _run(
        lambda repository, current, client, subject: collect_range(
            repository, current, client=client, subject_key=subject, windows=windows
        ),
        event="fitbit_sync",
    )
