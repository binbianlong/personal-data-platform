from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.screen_time.audit import audit_source
from personal_data_platform.sources.screen_time.collector_state import CollectorScan

NOW = datetime(2026, 10, 5, tzinfo=UTC)
DEVICE = "a" * 64


class Repository:
    def __init__(self, completed_at):
        self.scan = CollectorScan(completed_at, (DEVICE,), {DEVICE: completed_at})

    def last_successful_scan(self):
        return self.scan


@pytest.mark.parametrize("age_hours,healthy", [(25, True), (48, True), (49, False)])
def test_collector_sleep_is_reported_only_after_48_hours(age_hours, healthy) -> None:
    health = audit_source(Repository(NOW - timedelta(hours=age_hours)), [], NOW)
    assert health.ok is healthy
    assert health.details["collector_scan_stale"] is (not healthy)
    assert health.details["stale_collector_count"] == (0 if healthy else 1)
