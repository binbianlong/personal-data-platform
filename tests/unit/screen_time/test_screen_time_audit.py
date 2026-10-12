from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.screen_time.audit import audit_source
from personal_data_platform.sources.screen_time.raw import (
    CollectorDeviceManifest,
    CollectorScanReceipt,
)

NOW = datetime(2026, 10, 5, tzinfo=UTC)
DEVICE = "a" * 64


class Repository:
    def __init__(self, stream, completed_at):
        self.manifest = CollectorDeviceManifest((DEVICE,), completed_at, stream=stream)
        self.receipt = CollectorScanReceipt(DEVICE, completed_at, 1, stream=stream)

    def get_device_manifest(self):
        return self.manifest

    def list_scan_receipts(self):
        return [self.receipt]


@pytest.mark.parametrize("stream", ["app-in-focus", "app-usage"])
@pytest.mark.parametrize("age_hours,healthy", [(25, True), (48, True), (49, False)])
def test_collector_sleep_is_reported_only_after_48_hours(stream, age_hours, healthy) -> None:
    health = audit_source(Repository(stream, NOW - timedelta(hours=age_hours)), [], NOW)
    assert health.ok is healthy
    assert health.details["collector_manifest_stale"] is (not healthy)
    assert health.details["stale_collector_count"] == (0 if healthy else 1)
