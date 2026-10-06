"""Reconcile lifecycle-managed GCS Raw with warehouse ingestion state."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from personal_data_platform.config import schema_profile
from personal_data_platform.loader.deadline import interrupt_after
from personal_data_platform.loader.job import (
    LOADER_LEASE_SECONDS,
    run_loader,
    run_loader_objects,
)
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import (
    RawRepository,
    SourceAdapter,
    list_source_raw,
    selected_prefixes,
    validate_runtime_policy,
)
from personal_data_platform.sources.registry import get_source, get_sources
from personal_data_platform.storage.motherduck import (
    IngestionState,
    Warehouse,
    WarehouseConfig,
    connect,
)

from .heartbeat import HeartbeatPublisher, daily_heartbeat_urls, publish_http_heartbeat
from .models import ReconciliationResult

LOGGER = logging.getLogger(__name__)


def _no_external_heartbeat(_payload: dict[str, object]) -> None:
    """Cloud Monitoring observes the Job completion instead of an HTTP ping."""


def _relation_names(warehouse: Warehouse) -> set[str]:
    rows = warehouse.query_rows(
        """
        SELECT table_schema || '.' || table_name
        FROM information_schema.tables
        WHERE table_catalog = current_database()
          AND table_schema IN ('base', 'marts', 'ops')
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
    raw_objects: tuple[RawObject, ...] | None = None,
    _lease_owner: str | None = None,
    publish_success: bool = True,
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
    supplied_inventory = raw_objects is not None
    if _lease_owner is not None:
        warehouse.require_job_lock(_lease_owner)
    raw_by_key = (
        {value.key: value for value in raw_objects}
        if raw_objects is not None
        else _list_raw_by_key(repository, source, prefix)
    )
    raw_refs = list(raw_by_key.values())
    raw_keys = set(raw_by_key)
    parser_version = source.parser_version
    loaded_keys = warehouse.succeeded_keys_for(raw_refs, parser_version=parser_version)
    missing_before_repair = raw_keys - loaded_keys
    repair_summary: dict[str, int] | None = None
    if missing_before_repair and repair_missing:
        if _lease_owner is None:
            summary = run_loader(repository, warehouse, source=source, prefix=prefix)
        else:
            summary = run_loader(
                repository, warehouse, source=source, prefix=prefix, _lease_owner=_lease_owner
            )
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
    if not supplied_inventory and set(states) - raw_keys:
        latest_raw_by_key = _list_raw_by_key(repository, source, prefix)
        for key in set(states):
            if key in latest_raw_by_key:
                raw_by_key[key] = latest_raw_by_key[key]
        raw_refs = list(raw_by_key.values())
        raw_keys = set(raw_by_key)
        states = warehouse.active_ingestion_states(source_id=source.source_id, stream=source.stream)

    checked_at = started_at if now is not None else datetime.now(UTC)
    succeeded_keys = warehouse.succeeded_keys_for(raw_refs, parser_version=parser_version)
    failed_keys = _active_status_keys(states, "failed")
    loading_keys = _active_status_keys(states, "loading")
    live_loaded_keys = raw_keys & succeeded_keys
    missing = raw_keys - succeeded_keys

    expected_expired: set[str] = set()
    premature_missing: set[str] = set()
    unrecoverable_uningested: set[str] = set()
    unknown_creation_time: set[str] = set()
    for key, state in states.items():
        created_at = _aware_utc(state.retention_started_at or state.storage_created_at)
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
        created_at = _aware_utc(value.retention_origin)
        if created_at is None:
            unknown_creation_time.add(key)
        elif created_at <= checked_at - lifecycle_overdue:
            overdue_deletion.add(key)
        elif created_at <= checked_at - raw_retention:
            lifecycle_lag.add(key)

    source_health = source.audit(repository, raw_refs, checked_at)
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
        if _lease_owner is not None:
            warehouse.require_job_lock(_lease_owner)
        stage = "retention_expiry"
        marked_expired = warehouse.mark_retention_expired(
            (states[key] for key in expected_expired),
            expired_at=checked_at,
        )
        if marked_expired != expected_expired:
            raise RuntimeError("retention state changed during reconciliation; retry the audit")
        stage = "warehouse_heartbeat"
        if publish_success:
            warehouse.publish_heartbeat(source.monitor_name, result.run_id, heartbeat_payload)
        stage = "heartbeat"
        if publish_success:
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


def run_reconciliation_from_env(
    *, source_id: str | None = None, stream: str | None = None, all_streams: bool = False
) -> int:
    schema_profile()
    return _run_daily_reconciliation()


def _run_daily_reconciliation() -> int:
    from datetime import date
    from zoneinfo import ZoneInfo

    from personal_data_platform.dbt_runner import run_dbt_from_env
    from personal_data_platform.sources.fitbit.logging import configure_logging
    from personal_data_platform.sources.fitbit.runtime import run_daily_repair

    configure_logging("personal_data_platform.reconciliation")
    urls = daily_heartbeat_urls()
    sources = get_sources("screen_time", all_streams=True)
    for source in sources:
        validate_runtime_policy(source)
    warehouse = Warehouse(connect(WarehouseConfig.from_env()))
    owner = str(uuid.uuid4())
    now = datetime.now(UTC)
    target_date = now.astimezone(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    deadline = time.monotonic() + 100 * 60
    acquired = job_started = completed = False
    timer = None
    stage = "startup"
    details: dict[str, object] = {"planned_target_date": target_date.isoformat()}

    def remaining() -> int:
        seconds = int(deadline - time.monotonic())
        if seconds <= 0:
            raise TimeoutError("daily reconciliation deadline exceeded")
        warehouse.require_job_lock(owner, remaining_seconds=seconds)
        return seconds

    try:
        warehouse.migrate(profile=schema_profile())
        acquired = warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS)
        if not acquired:
            LOGGER.info(
                "daily reconciliation deferred: shared loader busy",
                extra={"event": "reconciliation", "status": "deferred"},
            )
            return 0
        timer = interrupt_after(warehouse, remaining())
        warehouse.begin_job("daily-reconciliation-west", owner)
        job_started = True
        previous = warehouse.query_value(
            "SELECT json_extract_string(details,'$.last_completed_target_date') FROM ops.job_run WHERE job_name='daily-reconciliation-west' AND json_extract_string(details,'$.last_completed_target_date') IS NOT NULL ORDER BY completed_at DESC LIMIT 1"
        )
        if previous:
            first_missing = date.fromisoformat(str(previous)) + timedelta(days=1)
            repair_start = target_date - timedelta(days=6)
            if first_missing < repair_start:
                gap = {
                    "from": first_missing.isoformat(),
                    "through": (repair_start - timedelta(days=1)).isoformat(),
                }
                details["manual_repair_required"] = gap
                LOGGER.warning(
                    "manual Fitbit repair required",
                    extra={"event": "fitbit_manual_repair", **gap},
                )
        inventories = []
        for source in sources:
            stage = "loader:" + source.stream
            remaining()
            repository = source.repository_from_env()
            refs = tuple(list_source_raw(repository, source))
            inventories.append((source, repository, refs))
            if not run_loader_objects(
                repository, warehouse, refs, source=source, _lease_owner=owner, _deadline=deadline
            ).ok:
                return 1
        stage = "fitbit"
        repair = run_daily_repair(
            now=now, warehouse=warehouse, lease_owner=owner, timeout_seconds=remaining()
        )
        if not repair.ok:
            return 1
        stage = "dbt"
        if run_dbt_from_env(lease_owner=owner, timeout_seconds=remaining()):
            return 1
        for source, repository, refs in inventories:
            stage = "audit:" + source.stream
            remaining()
            result = run_reconciliation(
                repository,
                warehouse,
                source=source,
                raw_objects=refs,
                repair_missing=False,
                heartbeat=_no_external_heartbeat,
                _lease_owner=owner,
                publish_success=False,
            )
            if not result.ok:
                return 1
        remaining()
        payload: dict[str, object] = {
            "run_id": owner,
            "completed_at": datetime.now(UTC).isoformat(),
            "job_name": "daily-reconciliation-west",
            "status": "succeeded",
        }
        stage = "completion"
        details["last_completed_target_date"] = target_date.isoformat()
        warehouse.connection.execute("BEGIN TRANSACTION")
        try:
            for name in ("daily", *(source.monitor_name for source in sources)):
                warehouse.publish_heartbeat(name, owner, payload)
            warehouse.finish_job(owner, succeeded=True, details=details)
            remaining()
            warehouse.connection.execute("COMMIT")
        except Exception:
            warehouse.connection.execute("ROLLBACK")
            raise
        completed = True
        stage = "heartbeat"
        remaining()
        publish_http_heartbeat(urls["daily"], {**payload, "monitor": "daily"})
        LOGGER.info(
            "daily reconciliation succeeded",
            extra={
                "event": "reconciliation",
                "status": "succeeded",
                "job_name": "daily-reconciliation-west",
            },
        )
        return 0
    except Exception:
        completed = False
        LOGGER.exception(
            "daily reconciliation failed",
            extra={
                "event": "reconciliation",
                "status": "failed",
                "job_name": "daily-reconciliation-west",
            },
        )
        return 1
    finally:
        if timer is not None:
            timer.cancel()
        if acquired and warehouse.connection_usable:
            if job_started and not completed:
                warehouse.finish_job(
                    owner, succeeded=False, details={**details, "failed_stage": stage}
                )
            warehouse.release_job_lock("loader", owner)
        warehouse.close()
