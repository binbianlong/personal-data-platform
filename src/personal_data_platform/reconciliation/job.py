"""Reconcile lifecycle-managed GCS Raw with warehouse ingestion state."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from personal_data_platform.loader.job import run_loader
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import (
    RawRepository,
    SourceAdapter,
    list_source_raw,
    selected_prefixes,
    validate_runtime_policy,
)
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import (
    IngestionState,
    Warehouse,
    WarehouseConfig,
    connect,
)

from .heartbeat import HeartbeatPublisher, publish_http_heartbeat
from .models import ReconciliationResult

LOGGER = logging.getLogger(__name__)
RECONCILIATION_LEASE_SECONDS = 65 * 60


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


def _failed_relation_queries(
    warehouse: Warehouse, relations: set[str], required_relations: tuple[str, ...]
) -> tuple[str, ...]:
    failed: list[str] = []
    for relation in required_relations:
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


def _list_raw_by_key(
    repository: RawRepository, source: SourceAdapter, prefix: str | None
) -> dict[str, RawObject]:
    return {value.key: value for value in list_source_raw(repository, source, prefix)}


def run_reconciliation(
    repository: RawRepository,
    warehouse: Warehouse,
    *,
    heartbeat: HeartbeatPublisher,
    source: SourceAdapter | None = None,
    prefix: str | None = None,
    repair_missing: bool = True,
    now: datetime | None = None,
) -> ReconciliationResult:
    """Audit raw/warehouse parity, repair missing loads, and publish success."""

    source = source or get_source()
    if selected_prefixes(source, prefix) != source.raw_prefixes:
        raise ValueError("reconciliation must audit every canonical prefix for the source stream")
    raw_retention = timedelta(days=source.retention_days)
    lifecycle_overdue = timedelta(days=source.retention_days + source.lifecycle_grace_days)
    started_at = _aware_utc(now or datetime.now(UTC))
    if started_at is None:
        raise ValueError("reconciliation time must be timezone-aware")
    run_id = str(uuid.uuid4())
    raw_by_key = _list_raw_by_key(repository, source, prefix)
    raw_objects = list(raw_by_key.values())
    raw_keys = set(raw_by_key)
    parser_version = getattr(source, "parser_version", None)
    loaded_keys = warehouse.succeeded_keys_for(raw_objects, parser_version=parser_version)
    missing_before_repair = raw_keys - loaded_keys
    repair_summary: dict[str, Any] | None = None
    if missing_before_repair and repair_missing:
        summary = run_loader(repository, warehouse, source=source, prefix=prefix)
        repair_summary = {
            "discovered": summary.discovered,
            "skipped": summary.skipped,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "records": summary.records,
        }
    states = warehouse.active_ingestion_states(source_id=source.source_id, stream=source.stream)
    # A repair or concurrent loader can commit keys newer than the initial listing.
    # Refresh before classifying any active warehouse key as absent from GCS.
    if set(states) - raw_keys:
        latest_raw_by_key = _list_raw_by_key(repository, source, prefix)
        for key in set(states):
            if key in latest_raw_by_key:
                raw_by_key[key] = latest_raw_by_key[key]
        raw_objects = list(raw_by_key.values())
        raw_keys = set(raw_by_key)
        states = warehouse.active_ingestion_states(source_id=source.source_id, stream=source.stream)

    checked_at = started_at if now is not None else datetime.now(UTC)
    succeeded_keys = warehouse.succeeded_keys_for(raw_objects, parser_version=parser_version)
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
        elif created_at <= checked_at - raw_retention:
            expected_expired.add(key)
        else:
            premature_missing.add(key)

    lifecycle_lag: set[str] = set()
    overdue_deletion: set[str] = set()
    for key, value in raw_by_key.items():
        created_at = _aware_utc(getattr(value, "storage_created_at", None))
        if created_at is None:
            unknown_creation_time.add(key)
        elif created_at <= checked_at - lifecycle_overdue:
            overdue_deletion.add(key)
        elif created_at <= checked_at - raw_retention:
            lifecycle_lag.add(key)

    source_health = source.audit(repository, raw_objects, checked_at)
    unexpected_absent_succeeded = premature_missing | (
        (unknown_creation_time & succeeded_keys) - raw_keys
    )
    failed = len(failed_keys)
    loading = len(loading_keys)
    inventory_counts = warehouse.retention_inventory_counts(
        source_id=source.source_id, stream=source.stream
    )
    available_relations = _relation_names(warehouse)
    missing_relations = tuple(sorted(set(source.required_relations) - available_relations))
    failed_relation_queries = _failed_relation_queries(
        warehouse, available_relations, source.required_relations
    )
    succeeded = (
        source_health.ok
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
        **source_health.details,
        "source_id": source.source_id,
        "stream": source.stream,
        "schema_versions": list(source.schema_versions),
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
        "missing_relations": list(missing_relations),
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
        missing_relations=missing_relations,
        failed_relation_queries=failed_relation_queries,
        details=details,
    )
    if not result.ok:
        warehouse.record_reconciliation(result)
        return result

    heartbeat_payload = {
        "source_id": source.source_id,
        "stream": source.stream,
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
        warehouse.publish_heartbeat(source.monitor_name, result.run_id, heartbeat_payload)
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


def run_reconciliation_from_env(*, source_id: str | None = None, stream: str | None = None) -> int:

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    heartbeat_url = os.environ.get("RECONCILIATION_HEARTBEAT_URL")
    if not heartbeat_url:
        raise ValueError("RECONCILIATION_HEARTBEAT_URL is required")
    source = get_source(source_id=source_id, stream=stream)
    validate_runtime_policy(source)
    repository = source.repository_from_env()
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
                source=source,
            )
        finally:
            warehouse.release_job_lock("reconciliation", owner_id)
        LOGGER.info(
            "reconciliation status=%s raw=%d loaded=%d missing=%d failed=%d "
            "orphaned=%d source=%s stream=%s",
            result.status,
            result.raw_object_count,
            result.loaded_object_count,
            result.missing_object_count,
            result.failed_object_count,
            result.orphaned_loaded_object_count,
            source.source_id,
            source.stream,
        )
        return 0 if result.ok else 1
    finally:
        warehouse.close()
