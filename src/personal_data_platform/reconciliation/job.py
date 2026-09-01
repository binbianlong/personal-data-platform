"""Reconcile lifecycle-managed GCS Raw with warehouse ingestion state."""

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
    run_loader,
)
from personal_data_platform.storage.motherduck import (
    IngestionState,
    Warehouse,
    WarehouseConfig,
    connect,
)

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
RAW_RETENTION = timedelta(days=60)
LIFECYCLE_OVERDUE = timedelta(days=63)


def _relation_names(warehouse: Warehouse) -> set[str]:
    rows = warehouse.query_rows(
        """
        SELECT table_schema || '.' || table_name
        FROM information_schema.tables
        WHERE table_catalog = current_database()
          AND table_schema IN ('base', 'marts')
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


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def _active_status_keys(states: dict[str, IngestionState], status: str) -> set[str]:
    return {key for key, value in states.items() if value.status == status}


def _list_raw_by_key(repository: RawRepository, prefix: str) -> dict[str, Any]:
    raw_objects = list(repository.list_raw(prefix))
    raw_by_key = {value.key: value for value in raw_objects}
    if len(raw_by_key) != len(raw_objects):
        raise RuntimeError("raw object listing returned duplicate keys")
    return raw_by_key


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

    started_at = _aware_utc(now or datetime.now(UTC))
    if started_at is None:
        raise ValueError("reconciliation time must be timezone-aware")
    run_id = str(uuid.uuid4())
    raw_by_key = _list_raw_by_key(repository, prefix)
    raw_objects = list(raw_by_key.values())
    raw_keys = set(raw_by_key)
    loaded_keys = warehouse.succeeded_keys_for(raw_objects)
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
    states = warehouse.active_ingestion_states()
    # A repair or concurrent loader can commit keys newer than the initial listing.
    # Refresh before classifying any active warehouse key as absent from GCS.
    if set(states) - raw_keys:
        latest_raw_by_key = _list_raw_by_key(repository, prefix)
        for key in set(states):
            if key in latest_raw_by_key:
                raw_by_key[key] = latest_raw_by_key[key]
        raw_objects = list(raw_by_key.values())
        raw_keys = set(raw_by_key)
        states = warehouse.active_ingestion_states()

    checked_at = started_at if now is not None else datetime.now(UTC)
    succeeded_keys = {
        key
        for key, state in states.items()
        if state.status == "succeeded"
        and key in raw_by_key
        and state.storage_generation == raw_by_key[key].storage_generation
    }
    failed_keys = _active_status_keys(states, "failed")
    loading_keys = _active_status_keys(states, "loading")
    live_loaded_keys = raw_keys & succeeded_keys
    missing = raw_keys - succeeded_keys

    expected_expired: set[str] = set()
    premature_missing: set[str] = set()
    unrecoverable_uningested: set[str] = set()
    unknown_creation_time: set[str] = set()
    for key, state in states.items():
        created_at = _aware_utc(state.storage_created_at)
        if key in raw_keys:
            if created_at is None:
                unknown_creation_time.add(key)
            continue
        if state.status != "succeeded":
            unrecoverable_uningested.add(key)
        elif created_at is None:
            unknown_creation_time.add(key)
        elif created_at <= checked_at - RAW_RETENTION:
            expected_expired.add(key)
        else:
            premature_missing.add(key)

    lifecycle_lag: set[str] = set()
    overdue_deletion: set[str] = set()
    for key, value in raw_by_key.items():
        created_at = _aware_utc(getattr(value, "storage_created_at", None))
        if created_at is None:
            unknown_creation_time.add(key)
        elif created_at <= checked_at - LIFECYCLE_OVERDUE:
            overdue_deletion.add(key)
        elif created_at <= checked_at - RAW_RETENTION:
            lifecycle_lag.add(key)

    raw_device_keys = {value.device_key for value in raw_by_key.values()}
    device_manifest = repository.get_device_manifest()
    configured_device_keys = (
        set(device_manifest.device_keys) if device_manifest is not None else set()
    )
    # The manifest is written only after a complete allowlist scan and is the
    # authoritative active-device set. Retained Raw from a removed device must
    # not keep requiring a fresh receipt until lifecycle deletion catches up.
    expected_device_keys = configured_device_keys
    retained_inactive_device_keys = raw_device_keys - configured_device_keys
    receipts = list(repository.list_scan_receipts())
    receipt_device_keys = {value.device_key for value in receipts}
    missing_receipt_devices = expected_device_keys - receipt_device_keys
    stale_receipts = [
        value
        for value in receipts
        if value.device_key in expected_device_keys
        and (
            value.completed_at < checked_at - COLLECTOR_FRESHNESS
            or value.completed_at > checked_at + MAX_CLOCK_SKEW
        )
    ]
    manifest_stale = device_manifest is not None and (
        device_manifest.completed_at < checked_at - COLLECTOR_FRESHNESS
        or device_manifest.completed_at > checked_at + MAX_CLOCK_SKEW
    )
    unexpected_absent_succeeded = premature_missing | (
        (unknown_creation_time & succeeded_keys) - raw_keys
    )
    failed = len(failed_keys)
    loading = len(loading_keys)
    inventory_counts = warehouse.retention_inventory_counts()
    available_relations = _relation_names(warehouse)
    missing_relations = tuple(sorted(set(REQUIRED_RELATIONS) - available_relations))
    failed_relation_queries = _failed_relation_queries(warehouse, available_relations)
    succeeded = (
        device_manifest is not None
        and not manifest_stale
        and not missing_receipt_devices
        and not stale_receipts
        and not missing
        and not premature_missing
        and not unrecoverable_uningested
        and not unknown_creation_time
        and not overdue_deletion
        and failed == 0
        and loading == 0
        and not missing_relations
        and not failed_relation_queries
    )
    completed_at = datetime.now(UTC)
    existing_expired_count = inventory_counts["expired_object_count"]
    details: dict[str, object] = {
        "missing_before_repair": len(missing_before_repair),
        "repair_summary": repair_summary,
        "total_object_count": inventory_counts["total_object_count"],
        "expired_object_count": (
            existing_expired_count + len(expected_expired) if succeeded else existing_expired_count
        ),
        "newly_expired_object_count": len(expected_expired) if succeeded else 0,
        "expected_expiry_candidate_count": len(expected_expired),
        "premature_missing_object_count": len(premature_missing),
        "unrecoverable_uningested_object_count": len(unrecoverable_uningested),
        "unknown_creation_time_object_count": len(unknown_creation_time),
        "overdue_deletion_object_count": len(overdue_deletion),
        "lifecycle_lag_object_count": len(lifecycle_lag),
        "live_loaded_object_count": len(live_loaded_keys),
        "live_unloaded_object_count": len(missing),
        "active_loading_object_count": loading,
        "orphaned_loaded_object_count": len(unexpected_absent_succeeded),
        "collector_receipt_count": len(receipts),
        "collector_manifest_present": device_manifest is not None,
        "collector_manifest_stale": manifest_stale,
        "configured_collector_device_count": len(configured_device_keys),
        "retained_inactive_device_count": len(retained_inactive_device_keys),
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
        loaded_object_count=len(live_loaded_keys),
        missing_object_count=len(missing),
        failed_object_count=failed,
        orphaned_loaded_object_count=len(unexpected_absent_succeeded),
        collector_receipt_count=len(receipts),
        stale_collector_count=len(stale_receipts),
        missing_collector_receipt_count=len(missing_receipt_devices),
        missing_relations=missing_relations,
        failed_relation_queries=failed_relation_queries,
        details=details,
    )
    if not result.ok:
        warehouse.record_reconciliation(result)
        return result

    heartbeat_payload = {
        "run_id": result.run_id,
        "completed_at": result.completed_at.isoformat(),
        "raw_object_count": result.raw_object_count,
        "loaded_object_count": result.loaded_object_count,
        "expired_object_count": details["expired_object_count"],
    }
    warehouse.record_reconciliation(replace(result, status="running"))
    warehouse.connection.execute("BEGIN TRANSACTION")
    stage = "warehouse_heartbeat"
    try:
        stage = "retention_expiry"
        marked_expired = warehouse.mark_retention_expired(
            (states[key] for key in expected_expired),
            expired_at=checked_at,
        )
        if marked_expired != expected_expired:
            raise RuntimeError("retention state changed during reconciliation; retry the audit")
        stage = "warehouse_heartbeat"
        warehouse.publish_heartbeat("screen_time_reconciliation", result.run_id, heartbeat_payload)
        stage = "heartbeat"
        heartbeat(heartbeat_payload)
        stage = "completion"
        warehouse.record_reconciliation(result)
        warehouse.connection.execute("COMMIT")
    except Exception as error:
        warehouse.connection.execute("ROLLBACK")
        result = replace(
            result,
            status="failed",
            details={
                **result.details,
                "expired_object_count": existing_expired_count,
                "newly_expired_object_count": 0,
                f"{stage}_error": str(error),
            },
        )
        warehouse.record_reconciliation(result)
    return result


def run_reconciliation_from_env() -> int:
    from personal_data_platform.storage.gcs import GCSRawRepository

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    heartbeat_url = os.environ.get("RECONCILIATION_HEARTBEAT_URL")
    if not heartbeat_url:
        raise ValueError("RECONCILIATION_HEARTBEAT_URL is required")
    repository = GCSRawRepository.from_env()
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
                prefix=RAW_PREFIX,
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
