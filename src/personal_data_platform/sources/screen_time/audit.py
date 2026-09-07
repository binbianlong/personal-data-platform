"""Screen Time collector liveness checks for the common reconciliation job."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Protocol, cast

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawRepository, SourceHealth
from personal_data_platform.sources.screen_time.raw import (
    CollectorDeviceManifest,
    CollectorScanReceipt,
)

COLLECTOR_FRESHNESS = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=10)


class _CollectorRepository(Protocol):
    def get_device_manifest(self) -> CollectorDeviceManifest | None: ...

    def list_scan_receipts(self) -> Iterable[CollectorScanReceipt]: ...


def audit_source(
    repository: RawRepository, observations: Sequence[RawObject], now: datetime
) -> SourceHealth:
    collector = cast(_CollectorRepository, repository)
    raw_device_keys = {value.subject_key for value in observations}
    device_manifest = collector.get_device_manifest()
    configured_device_keys = (
        set(device_manifest.device_keys) if device_manifest is not None else set()
    )
    # The manifest is written after a complete allowlist scan and defines the
    # active set. Retained Raw from a removed device needs no fresh receipt.
    retained_inactive_device_keys = raw_device_keys - configured_device_keys
    receipts = list(collector.list_scan_receipts())
    receipt_device_keys = {value.device_key for value in receipts}
    missing_receipt_devices = configured_device_keys - receipt_device_keys
    stale_receipts = [
        value
        for value in receipts
        if value.device_key in configured_device_keys
        and (
            value.completed_at < now - COLLECTOR_FRESHNESS
            or value.completed_at > now + MAX_CLOCK_SKEW
        )
    ]
    manifest_stale = device_manifest is not None and (
        device_manifest.completed_at < now - COLLECTOR_FRESHNESS
        or device_manifest.completed_at > now + MAX_CLOCK_SKEW
    )
    return SourceHealth(
        ok=(
            device_manifest is not None
            and not manifest_stale
            and not missing_receipt_devices
            and not stale_receipts
        ),
        details={
            "collector_receipt_count": len(receipts),
            "collector_manifest_present": device_manifest is not None,
            "collector_manifest_stale": manifest_stale,
            "configured_collector_device_count": len(configured_device_keys),
            "retained_inactive_device_count": len(retained_inactive_device_keys),
            "stale_collector_count": len(stale_receipts),
            "missing_collector_receipt_count": len(missing_receipt_devices),
            "missing_collector_receipt_devices": sorted(missing_receipt_devices),
            "stale_collector_receipt_devices": sorted(value.device_key for value in stale_receipts),
        },
    )
