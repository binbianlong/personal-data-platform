"""Reconcile OwnTracks Raw IDs and record daily outcomes in JSON logs."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta

from personal_data_platform.loader.deadline import interrupt_after
from personal_data_platform.loader.job import LOADER_LEASE_SECONDS, run_loader_objects
from personal_data_platform.logging_config import configure_logging
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import (
    RawRepository,
    SourceAdapter,
    list_source_raw,
    selected_prefixes,
    validate_observations,
    validate_runtime_policy,
)
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.factory import WarehouseConfig, open_warehouse
from personal_data_platform.storage.warehouse import Warehouse

from .heartbeat import daily_heartbeat_urls, publish_http_heartbeat
from .models import DAILY_JOB_NAME, ReconciliationResult

LOGGER = logging.getLogger(__name__)


def _relation_names(warehouse: Warehouse) -> set[str]:
    return warehouse.relation_names()


def _failed_relation_queries(
    warehouse: Warehouse, relations: set[str], required_relations: tuple[str, ...]
) -> tuple[str, ...]:
    failed = []
    for relation in required_relations:
        if relation not in relations:
            continue
        try:
            warehouse.check_relation(relation)
        except Exception:
            LOGGER.exception("required relation query failed: %s", relation)
            failed.append(relation)
    return tuple(failed)


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
    """Compare complete GCS logical IDs directly with base, repairing missing points."""
    source = source or get_source("owntracks")
    if source.source_id != "owntracks":
        raise ValueError(
            "Raw reconciliation supports OwnTracks; Screen Time history is canonical in base"
        )
    if selected_prefixes(source, prefix) != source.raw_prefixes:
        raise ValueError("reconciliation must audit every canonical prefix for the source stream")
    started_at = now or datetime.now(UTC)
    if started_at.tzinfo is None:
        raise ValueError("reconciliation time must be timezone-aware")
    if _lease_owner is not None:
        warehouse.require_job_lock(_lease_owner)
    refs = (
        list(validate_observations(source, raw_objects))
        if raw_objects is not None
        else list_source_raw(repository, source, prefix)
    )
    raw_ids = {raw.logical_key for raw in refs}
    loaded_ids = warehouse.location_point_ids()
    missing_before = raw_ids - loaded_ids
    repair_summary = None
    if missing_before and repair_missing:
        summary = run_loader_objects(
            repository, warehouse, refs, source=source, _lease_owner=_lease_owner
        )
        repair_summary = asdict(summary)
    loaded_ids = warehouse.location_point_ids()
    # A worker may have loaded an upload newer than the initial inventory.
    # Refresh only these candidate orphan IDs, without reconstructing GCS keys.
    if raw_objects is None and loaded_ids - raw_ids:
        raw_ids |= {
            raw.logical_key
            for raw in list_source_raw(repository, source, prefix)
            if raw.logical_key in loaded_ids
        }
    missing, orphaned = raw_ids - loaded_ids, loaded_ids - raw_ids
    failed = int(repair_summary["failed"]) if repair_summary else 0
    health = source.audit(repository, refs, started_at)
    available = warehouse.relation_names()
    missing_relations = tuple(sorted(set(source.required_relations) - available))
    failed_queries = _failed_relation_queries(warehouse, available, source.required_relations)
    ok = health.ok and not (missing or orphaned or failed or missing_relations or failed_queries)
    details: dict[str, object] = {
        **health.details,
        "source_id": source.source_id,
        "stream": source.stream,
        "missing_before_repair": len(missing_before),
        "repair_summary": repair_summary,
        "total_object_count": len(loaded_ids),
        "missing_raw_object_count": len(orphaned),
        "live_loaded_object_count": len(raw_ids & loaded_ids),
        "live_unloaded_object_count": len(missing),
        "orphaned_loaded_object_count": len(orphaned),
        "missing_relations": list(missing_relations),
        "failed_relation_queries": list(failed_queries),
    }
    return ReconciliationResult(
        run_id=str(uuid.uuid4()),
        status="succeeded" if ok else "failed",
        started_at=started_at,
        completed_at=datetime.now(UTC),
        raw_object_count=len(raw_ids),
        loaded_object_count=len(raw_ids & loaded_ids),
        missing_object_count=len(missing),
        failed_object_count=failed,
        orphaned_loaded_object_count=len(orphaned),
        missing_relations=missing_relations,
        failed_relation_queries=failed_queries,
        details=details,
    )


def run_reconciliation_from_env() -> int:
    from personal_data_platform.network_diagnostics import network_run

    with network_run("daily"):
        return _run_daily_reconciliation()


class _StageFailed(RuntimeError):
    """A stage returned an explicit failure outcome."""


def _run_daily_reconciliation() -> int:
    from zoneinfo import ZoneInfo

    from personal_data_platform.dbt_runner import run_dbt_from_env
    from personal_data_platform.network_diagnostics import network_phase
    from personal_data_platform.sources.fitbit.runtime import run_daily_repair

    owner = str(uuid.uuid4())
    now = datetime.now(UTC)
    target_date = now.astimezone(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    deadline = time.monotonic() + 100 * 60
    warehouse = None
    acquired = False
    timer = None
    stage, status = "startup", "failed"
    details: dict[str, object] = {
        "run_id": owner,
        "target_date": target_date.isoformat(),
        "started_at": now.isoformat(),
        "completed_at": None,
        "failed_stage": None,
    }

    def remaining() -> int:
        seconds = int(deadline - time.monotonic())
        if seconds <= 0:
            raise TimeoutError("daily reconciliation deadline exceeded")
        assert warehouse is not None
        warehouse.require_job_lock(owner, remaining_seconds=seconds)
        return seconds

    def event(value: str) -> None:
        # Execution history must survive filters for ordinary diagnostic logs.
        execution_logger = logging.getLogger(__name__ + ".execution")
        execution_logger.setLevel(logging.INFO)
        execution_logger.log(
            logging.ERROR if value == "failed" else logging.INFO,
            "daily reconciliation %s",
            value,
            extra={
                "event": "reconciliation",
                "status": value,
                "job_name": DAILY_JOB_NAME,
                "summary": details.copy(),
            },
        )

    try:
        configure_logging("personal_data_platform.reconciliation")
        event("running")
        urls = daily_heartbeat_urls()
        sources = (get_source("iphone_screen_time"), get_source("mac_screen_time"))
        owntracks = get_source("owntracks")
        for source in (*sources, owntracks):
            validate_runtime_policy(source)
        config = WarehouseConfig.from_env()
        warehouse = open_warehouse(config)
        stage = "migration"
        warehouse.migrate()
        stage = "lease"
        acquired = warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS)
        if not acquired:
            status = "deferred"
        else:
            timer = interrupt_after(warehouse, remaining())
            stage = "fitbit"
            with network_phase("daily_fitbit"):
                repair = run_daily_repair(
                    now=now, warehouse=warehouse, lease_owner=owner, timeout_seconds=remaining()
                )
            details["fitbit"] = (
                json.loads(json.dumps(asdict(repair), default=str))
                if is_dataclass(repair) and not isinstance(repair, type)
                else {"ok": repair.ok}
            )
            if not repair.ok:
                raise _StageFailed("Fitbit repair failed")
            stage = "dbt"
            with network_phase("daily_dbt"):
                dbt_status = run_dbt_from_env(
                    lease_owner=owner, timeout_seconds=remaining(), config=config
                )
            details["dbt"] = {"exit_status": dbt_status}
            if dbt_status:
                raise _StageFailed("dbt failed")
            stage = "owntracks"
            remaining()
            with network_phase("daily_owntracks"):
                owntracks_result = run_reconciliation(
                    owntracks.repository_from_env(), warehouse, source=owntracks, _lease_owner=owner
                )
            details["owntracks"] = owntracks_result.details
            if not owntracks_result.ok:
                raise _StageFailed("OwnTracks Raw reconciliation failed")
            for source in sources:
                stage = "relations:" + source.source_id
                remaining()
                with network_phase("daily_" + source.source_id):
                    relations = warehouse.relation_names()
                    missing = sorted(set(source.required_relations) - relations)
                    failed = _failed_relation_queries(
                        warehouse, relations, source.required_relations
                    )
                    details[source.source_id] = {
                        "missing_relations": missing,
                        "failed_relation_queries": list(failed),
                        "collector_liveness": "local_logs",
                    }
                    if missing or failed:
                        raise _StageFailed("required Screen Time relation validation failed")
            stage = "heartbeat"
            remaining()
            publish_http_heartbeat(
                urls["daily"],
                {
                    "run_id": owner,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "job_name": DAILY_JOB_NAME,
                    "status": "succeeded",
                    "monitor": "daily",
                },
            )
            status = "succeeded"
    except Exception:
        LOGGER.exception("daily stage failed: %s", stage)
        details["failed_stage"] = stage
    finally:
        if timer is not None:
            timer.cancel()
        if warehouse is not None:
            try:
                if acquired and warehouse.connection_usable:
                    warehouse.release_job_lock("loader", owner)
            except Exception:
                status = "failed"
                details["failed_stage"] = details["failed_stage"] or "release"
                LOGGER.exception("daily lease release failed")
            try:
                warehouse.close()
            except Exception:
                status = "failed"
                details["failed_stage"] = details["failed_stage"] or "close"
                LOGGER.exception("daily warehouse close failed")
        details["completed_at"] = datetime.now(UTC).isoformat()
        event(status)
    return 0 if status in {"succeeded", "deferred"} else 1
