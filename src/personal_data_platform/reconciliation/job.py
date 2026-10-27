"""Reconcile saved Raw with warehouse ingestion state."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from personal_data_platform.loader.deadline import interrupt_after
from personal_data_platform.loader.job import (
    LOADER_LEASE_SECONDS,
    run_loader,
)
from personal_data_platform.logging_config import configure_logging
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import (
    RawRepository,
    SourceAdapter,
    list_source_raw,
    selected_prefixes,
    validate_runtime_policy,
)
from personal_data_platform.sources.registry import get_source, get_sources
from personal_data_platform.sources.screen_time.audit import (
    audit_collector_heartbeat,
    filter_ingestion_states,
)
from personal_data_platform.storage.motherduck import (
    IngestionState,
    Warehouse,
    WarehouseConfig,
    connect,
)

from .heartbeat import daily_heartbeat_urls, publish_http_heartbeat
from .models import DAILY_JOB_HISTORY_NAMES, DAILY_JOB_NAME, ReconciliationResult

LOGGER = logging.getLogger(__name__)


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
    source: SourceAdapter | None = None,
    prefix: str | None = None,
    repair_missing: bool = True,
    now: datetime | None = None,
    raw_objects: tuple[RawObject, ...] | None = None,
    _lease_owner: str | None = None,
) -> ReconciliationResult:
    """Audit Raw/warehouse parity and repair missing loads."""

    source = source or get_source()
    if selected_prefixes(source, prefix) != source.raw_prefixes:
        raise ValueError("reconciliation must audit every canonical prefix for the source stream")
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

    if source.source_id == "screen_time":
        states = filter_ingestion_states(warehouse, states, raw_keys=raw_keys, stream=source.stream)
    checked_at = started_at if now is not None else datetime.now(UTC)
    succeeded_keys = warehouse.succeeded_keys_for(raw_refs, parser_version=parser_version)
    failed_keys = _active_status_keys(states, "failed")
    live_loaded_keys = raw_keys & succeeded_keys
    missing = raw_keys - succeeded_keys

    absent_loaded = _active_status_keys(states, "succeeded") - raw_keys
    absent_uningested = {
        key for key, value in states.items() if value.status != "succeeded"
    } - raw_keys
    source_health = source.audit(repository, raw_refs, checked_at)
    failed = len(failed_keys)
    available_relations = _relation_names(warehouse)
    missing_relations = tuple(sorted(set(source.required_relations) - available_relations))
    failed_relation_queries = _failed_relation_queries(
        warehouse, available_relations, source.required_relations
    )
    succeeded = (
        source_health.ok
        and not missing
        and not absent_loaded
        and not absent_uningested
        and failed == 0
        and not missing_relations
        and not failed_relation_queries
    )
    completed_at = datetime.now(UTC)
    details: dict[str, object] = {
        **source_health.details,
        "source_id": source.source_id,
        "stream": source.stream,
        "schema_versions": list(source.schema_versions),
        "missing_before_repair": len(missing_before_repair),
        "repair_summary": repair_summary,
        "total_object_count": len(states),
        "missing_raw_object_count": len(absent_loaded | absent_uningested),
        "unrecoverable_uningested_object_count": len(absent_uningested),
        "live_loaded_object_count": len(live_loaded_keys),
        "live_unloaded_object_count": len(missing),
        "orphaned_loaded_object_count": len(absent_loaded),
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
        orphaned_loaded_object_count=len(absent_loaded),
        missing_relations=missing_relations,
        failed_relation_queries=failed_relation_queries,
        details=details,
    )
    return result


def run_reconciliation_from_env() -> int:
    from personal_data_platform.network_diagnostics import network_run

    with network_run("daily"):
        return _run_daily_reconciliation()


def _run_daily_reconciliation() -> int:
    from datetime import date
    from zoneinfo import ZoneInfo

    from personal_data_platform.dbt_runner import run_dbt_from_env
    from personal_data_platform.network_diagnostics import network_phase
    from personal_data_platform.sources.fitbit.runtime import DAILY_REPAIR_DAYS, run_daily_repair

    configure_logging("personal_data_platform.reconciliation")
    urls = daily_heartbeat_urls()
    sources = get_sources("screen_time", all_streams=True)
    for source in sources:
        validate_runtime_policy(source)
    owntracks = get_source("owntracks")
    validate_runtime_policy(owntracks)
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
        warehouse.migrate()
        acquired = warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS)
        if not acquired:
            LOGGER.info(
                "daily reconciliation deferred: shared loader busy",
                extra={"event": "reconciliation", "status": "deferred"},
            )
            return 0
        timer = interrupt_after(warehouse, remaining())
        warehouse.begin_job(DAILY_JOB_NAME, owner)
        job_started = True
        previous = warehouse.query_value(
            "SELECT json_extract_string(details,'$.last_completed_target_date') "
            "FROM ops.job_run WHERE job_name IN (?, ?) AND status='succeeded' "
            "AND json_extract_string(details,'$.last_completed_target_date') IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 1",
            list(DAILY_JOB_HISTORY_NAMES),
        )
        if previous:
            first_missing = date.fromisoformat(str(previous)) + timedelta(days=1)
            repair_start = target_date - timedelta(days=DAILY_REPAIR_DAYS - 1)
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
        stage = "fitbit"
        with network_phase("daily_fitbit"):
            repair = run_daily_repair(
                now=now, warehouse=warehouse, lease_owner=owner, timeout_seconds=remaining()
            )
        if not repair.ok:
            return 1
        stage = "dbt"
        with network_phase("daily_dbt"):
            if run_dbt_from_env(lease_owner=owner, timeout_seconds=remaining()):
                return 1
        stage = "owntracks"
        remaining()
        with network_phase("daily_owntracks"):
            owntracks_result = run_reconciliation(
                owntracks.repository_from_env(),
                warehouse,
                source=owntracks,
                _lease_owner=owner,
            )
        details["owntracks"] = owntracks_result.details
        if not owntracks_result.ok:
            LOGGER.error("OwnTracks Raw reconciliation failed")
            return 1
        for source in sources:
            stage = "audit:" + source.stream
            remaining()
            with network_phase("daily_screen_time"):
                health = audit_collector_heartbeat(warehouse, source, datetime.now(UTC))
                relations = _relation_names(warehouse)
                missing_relations = sorted(set(source.required_relations) - relations)
                failed_queries = (
                    _failed_relation_queries(warehouse, relations, source.required_relations)
                    if health.ok and not missing_relations
                    else None
                )
                if not health.ok or missing_relations or failed_queries:
                    LOGGER.error(
                        "Screen Time audit failed: %s",
                        source.stream,
                        extra={
                            "event": "screen_time_audit",
                            "status": "failed",
                            "summary": {
                                "stream": source.stream,
                                **health.details,
                                "missing_relations": missing_relations,
                                "failed_relation_queries": list(failed_queries)
                                if failed_queries is not None
                                else None,
                            },
                        },
                    )
                    return 1
        remaining()
        payload: dict[str, object] = {
            "run_id": owner,
            "completed_at": datetime.now(UTC).isoformat(),
            "job_name": DAILY_JOB_NAME,
            "status": "succeeded",
        }
        stage = "completion"
        details["last_completed_target_date"] = target_date.isoformat()
        warehouse.connection.execute("BEGIN TRANSACTION")
        try:
            warehouse.prune_run_history(now=datetime.now(UTC))
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
                "job_name": DAILY_JOB_NAME,
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
                "job_name": DAILY_JOB_NAME,
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
