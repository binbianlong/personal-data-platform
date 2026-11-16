"""Run and monitor the daily data job."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta

from personal_data_platform import profile_steps
from personal_data_platform.loader.deadline import interrupt_after
from personal_data_platform.loader.job import LOADER_LEASE_SECONDS
from personal_data_platform.logging_config import configure_logging
from personal_data_platform.sources.contracts import validate_runtime_policy
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.factory import WarehouseConfig, open_warehouse

from .heartbeat import daily_heartbeat_urls, publish_http_heartbeat
from .reconciliation import check_required_relations, reconcile_owntracks

DAILY_JOB_NAME = "daily-reconciliation"
LOGGER = logging.getLogger(__name__)


def run_daily_from_env() -> int:
    from personal_data_platform.network_diagnostics import network_run

    with network_run("daily"):
        return _run_daily()


def _run_daily() -> int:
    from zoneinfo import ZoneInfo

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

    def record_failure(message: str) -> None:
        LOGGER.error("daily stage failed: %s (%s)", stage, message)
        details["failed_stage"] = details["failed_stage"] or stage

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
        configure_logging("personal_data_platform.daily")
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
                record_failure("Fitbit repair failed")
            stage = "owntracks"
            remaining()
            with network_phase("daily_owntracks"):
                owntracks_result = reconcile_owntracks(
                    owntracks.repository_from_env(), warehouse, source=owntracks, _lease_owner=owner
                )
            details["owntracks"] = owntracks_result.details
            if not owntracks_result.ok:
                record_failure("OwnTracks Raw reconciliation failed")
            for source in sources:
                stage = "relations:" + source.source_id
                remaining()
                with network_phase("daily_" + source.source_id):
                    missing, failed = check_required_relations(warehouse, source.required_relations)
                    details[source.source_id] = {
                        "missing_relations": list(missing),
                        "failed_relation_queries": list(failed),
                        "collector_liveness": "local_logs",
                    }
                    if missing or failed:
                        record_failure("required Screen Time relation validation failed")
            if details["failed_stage"] is not None:
                return 1
            if bucket := os.environ.get("PDP_PROFILE_BUCKET"):
                stage = "steps_heatmap"
                with network_phase("daily_steps_heatmap"):
                    profile_steps.publish(warehouse, target_date, bucket, timeout=remaining())
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
