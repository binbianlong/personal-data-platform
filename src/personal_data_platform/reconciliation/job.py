"""Reconcile immutable B2 raw objects with warehouse ingestion state."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from personal_data_platform.loader.job import (
    RAW_PREFIX,
    RawRepository,
    raw_prefix_from_env,
    run_loader,
)
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

from .heartbeat import HeartbeatPublisher, publish_http_heartbeat
from .models import ReconciliationResult

LOGGER = logging.getLogger(__name__)
REQUIRED_RELATIONS = (
    "base.screen_time_segment_observation",
    "base.screen_time_record_occurrence",
    "base.screen_time_transition",
    "base.screen_time_interval",
    "marts.daily_screen_time",
)
RECONCILIATION_LEASE_SECONDS = 65 * 60
COLLECTOR_FRESHNESS = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=10)


def _relation_names(warehouse: Warehouse) -> set[str]:
    rows = warehouse.query_rows(
        """
        SELECT table_schema || '.' || table_name
        FROM information_schema.tables
        WHERE table_schema IN ('base', 'marts')
        """
    )
    return {row[0] for row in rows}


def _failed_relation_queries(warehouse: Warehouse, relations: set[str]) -> tuple[str, ...]:
    failed: list[str] = []
    for relation in REQUIRED_RELATIONS:
        if relation not in relations:
            continue
        try:
            warehouse.query_value(f"SELECT count(*) FROM {relation}")
        except Exception:
            LOGGER.exception("required relation query failed: %s", relation)
            failed.append(relation)
    return tuple(failed)


def run_reconciliation(
    repository: RawRepository,
    warehouse: Warehouse,
    *,
    heartbeat: HeartbeatPublisher,
    prefix: str = RAW_PREFIX,
    repair_missing: bool = True,
    now: datetime | None = None,
) -> ReconciliationResult:
    """Audit raw/warehouse parity, repair missing loads, and publish success."""

    checked_at = now or datetime.now(UTC)
    started_at = checked_at
    run_id = str(uuid.uuid4())
    raw_objects = list(repository.list_raw(prefix))
    raw_keys = {value.key for value in raw_objects}
    raw_device_keys = {value.device_key for value in raw_objects}
    receipts = list(repository.list_scan_receipts())
    receipt_device_keys = {value.device_key for value in receipts}
    missing_receipt_devices = raw_device_keys - receipt_device_keys
    stale_receipts = [
        value
        for value in receipts
        if value.completed_at < checked_at - COLLECTOR_FRESHNESS
        or value.completed_at > checked_at + MAX_CLOCK_SKEW
    ]
    loaded_keys = warehouse.succeeded_keys()
    missing_before_repair = raw_keys - loaded_keys
    repair_summary: dict[str, Any] | None = None
    if missing_before_repair and repair_missing:
        summary = run_loader(repository, warehouse, prefix=prefix)
        repair_summary = {
            "discovered": summary.discovered,
            "skipped": summary.skipped,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "records": summary.records,
        }
        loaded_keys = warehouse.succeeded_keys()

    missing = raw_keys - loaded_keys
    orphaned = loaded_keys - raw_keys
    failed = warehouse.ingestion_counts().get("failed", 0)
    available_relations = _relation_names(warehouse)
    missing_relations = tuple(sorted(set(REQUIRED_RELATIONS) - available_relations))
    failed_relation_queries = _failed_relation_queries(warehouse, available_relations)
    succeeded = (
        bool(receipts)
        and not missing_receipt_devices
        and not stale_receipts
        and not missing
        and not orphaned
        and failed == 0
        and not missing_relations
        and not failed_relation_queries
    )
    completed_at = datetime.now(UTC)
    details: dict[str, object] = {
        "missing_before_repair": len(missing_before_repair),
        "repair_summary": repair_summary,
        "orphaned_loaded_object_count": len(orphaned),
        "collector_receipt_count": len(receipts),
        "stale_collector_count": len(stale_receipts),
        "missing_collector_receipt_count": len(missing_receipt_devices),
        "missing_relations": list(missing_relations),
        "missing_collector_receipt_devices": sorted(missing_receipt_devices),
        "stale_collector_receipt_devices": sorted(value.device_key for value in stale_receipts),
        "failed_relation_queries": list(failed_relation_queries),
    }
    result = ReconciliationResult(
        run_id=run_id,
        status="succeeded" if succeeded else "failed",
        started_at=started_at,
        completed_at=completed_at,
        raw_object_count=len(raw_keys),
        loaded_object_count=len(loaded_keys),
        missing_object_count=len(missing),
        failed_object_count=failed,
        orphaned_loaded_object_count=len(orphaned),
        collector_receipt_count=len(receipts),
        stale_collector_count=len(stale_receipts),
        missing_collector_receipt_count=len(missing_receipt_devices),
        missing_relations=missing_relations,
        failed_relation_queries=failed_relation_queries,
        details=details,
    )
    if result.ok:
        heartbeat_payload = {
            "run_id": result.run_id,
            "completed_at": result.completed_at.isoformat(),
            "raw_object_count": result.raw_object_count,
            "loaded_object_count": result.loaded_object_count,
        }
        try:
            heartbeat(heartbeat_payload)
        except Exception as error:
            result = replace(
                result,
                status="failed",
                details={**result.details, "heartbeat_error": str(error)},
            )
        else:
            warehouse.publish_heartbeat(
                "screen_time_reconciliation", result.run_id, heartbeat_payload
            )
    warehouse.record_reconciliation(result)
    return result


def run_reconciliation_from_env() -> int:
    from personal_data_platform.storage.b2 import B2RawRepository

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    heartbeat_url = os.environ.get("RECONCILIATION_HEARTBEAT_URL")
    if not heartbeat_url:
        raise ValueError("RECONCILIATION_HEARTBEAT_URL is required")
    repository = B2RawRepository.from_env()
    warehouse = Warehouse(connect(WarehouseConfig.from_env()))
    try:
        warehouse.migrate()
        owner_id = str(uuid.uuid4())
        if not warehouse.acquire_job_lock(
            "reconciliation", owner_id, lease_seconds=RECONCILIATION_LEASE_SECONDS
        ):
            raise RuntimeError("reconciliation already has an unexpired job lease")
        try:
            result = run_reconciliation(
                repository,
                warehouse,
                heartbeat=lambda payload: publish_http_heartbeat(heartbeat_url, payload),
                prefix=raw_prefix_from_env(),
            )
        finally:
            warehouse.release_job_lock("reconciliation", owner_id)
        LOGGER.info(
            "reconciliation status=%s raw=%d loaded=%d missing=%d failed=%d "
            "orphaned=%d stale_collectors=%d missing_receipts=%d",
            result.status,
            result.raw_object_count,
            result.loaded_object_count,
            result.missing_object_count,
            result.failed_object_count,
            result.orphaned_loaded_object_count,
            result.stale_collector_count,
            result.missing_collector_receipt_count,
        )
        return 0 if result.ok else 1
    finally:
        warehouse.close()
