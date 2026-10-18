"""Screen Time collector liveness checks for the common reconciliation job."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol, cast

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository, SourceHealth
from personal_data_platform.sources.screen_time.state import CollectorScan

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
