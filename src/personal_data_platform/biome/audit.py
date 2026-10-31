"""Screen Time collector liveness and ingestion-state audits."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast

from personal_data_platform.biome.collector_state import CollectorScan
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository, SourceAdapter, SourceHealth

if TYPE_CHECKING:
    from personal_data_platform.storage.warehouse import IngestionState, Warehouse

COLLECTOR_FRESHNESS = timedelta(hours=48)
MAX_CLOCK_SKEW = timedelta(minutes=10)


class _CollectorRepository(Protocol):
    def last_successful_scan(self) -> CollectorScan | None: ...


def audit_source(
    repository: RawRepository,
    observations: Sequence[RawObject],
    now: datetime,
    *,
    allow_inactive: bool = False,
    allow_unconfigured_empty: bool = False,
) -> SourceHealth:
    scan = cast(_CollectorRepository, repository).last_successful_scan()
    raw_device_keys = {value.subject_key for value in observations}
    configured_device_keys = set(scan.device_keys) if scan is not None else set()
    device_times = scan.device_scanned_at if scan is not None else {}
    missing_devices = configured_device_keys - device_times.keys()
    stale_devices = {
        key
        for key, completed_at in device_times.items()
        if key in configured_device_keys
        and not now - COLLECTOR_FRESHNESS <= completed_at <= now + MAX_CLOCK_SKEW
    }
    scan_stale = scan is not None and not (
        now - COLLECTOR_FRESHNESS <= scan.completed_at <= now + MAX_CLOCK_SKEW
    )
    inactive = (allow_unconfigured_empty and scan is None and not raw_device_keys) or (
        allow_inactive and scan is not None and not configured_device_keys
    )
    return SourceHealth(
        ok=inactive
        or (scan is not None and not scan_stale and not missing_devices and not stale_devices),
        details={
            "collector_inactive": inactive,
            "collector_device_count": len(device_times),
            "collector_scan_present": scan is not None,
            "collector_scan_stale": scan_stale,
            "configured_collector_device_count": len(configured_device_keys),
            "retained_inactive_device_count": len(raw_device_keys - configured_device_keys),
            "stale_collector_count": len(stale_devices),
            "missing_collector_scan_count": len(missing_devices),
            "missing_collector_scan_devices": sorted(missing_devices),
            "stale_collector_scan_devices": sorted(stale_devices),
        },
    )


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def audit_collector_heartbeat(
    warehouse: Warehouse, source: SourceAdapter, now: datetime
) -> SourceHealth:
    """Cloud jobs check only collector-owned success; they never renew it."""
    diagnostics: dict[str, object] = {"checked_at": now.isoformat()}
    heartbeat = warehouse.get_heartbeat(source.monitor_name)
    if heartbeat is None:
        return SourceHealth(False, {**diagnostics, "collector_heartbeat_missing": True})
    succeeded_at, serialized = heartbeat
    loaded_at = _aware_utc(succeeded_at)
    diagnostics["collector_succeeded_at"] = loaded_at.isoformat() if loaded_at else None
    try:
        details = json.loads(str(serialized))
        scan_at = _aware_utc(datetime.fromisoformat(details["scan_completed_at"]))
        diagnostics["scan_completed_at"] = scan_at.isoformat() if scan_at else None
        ok = all(
            value is not None and now - COLLECTOR_FRESHNESS <= value <= now + MAX_CLOCK_SKEW
            for value in (scan_at, loaded_at)
        )
        return SourceHealth(
            ok,
            {
                **diagnostics,
                "collector_inactive": details.get("collector_inactive") is True,
                "collector_heartbeat_stale": not ok,
            },
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return SourceHealth(False, {**diagnostics, "collector_heartbeat_invalid": True})


def filter_ingestion_states(
    warehouse: Warehouse,
    states: dict[str, IngestionState],
    *,
    raw_keys: set[str],
    source_id: str,
    stream: str,
) -> dict[str, IngestionState]:
    """Ignore compacted successes outside the live Raw and current segments."""
    current_keys = warehouse.current_biome_object_keys(source_id=source_id, stream=stream)
    return {
        key: value
        for key, value in states.items()
        if key in raw_keys or key in current_keys or value.status != "succeeded"
    }
