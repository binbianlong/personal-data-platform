from types import SimpleNamespace

import duckdb
import pytest

from personal_data_platform.reconciliation import job
from personal_data_platform.sources.fitbit import runtime
from personal_data_platform.sources.fitbit.daily import DailySummary
from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.raw import CollectorDeviceManifest
from personal_data_platform.storage.motherduck import Warehouse


@pytest.mark.parametrize("screen_time_failed", [False, True])
def test_daily_fitbit_shares_the_scheduled_warehouse_after_screen_time(
    monkeypatch, screen_time_failed
):
    warehouse = Warehouse(duckdb.connect(":memory:"))
    closed = []
    original_close = warehouse.close
    monkeypatch.setattr(warehouse, "close", lambda: closed.append(True))
    monkeypatch.setenv("PDP_RECONCILIATION_MONITORING_MODE", "cloud_monitoring")
    monkeypatch.delenv("RECONCILIATION_HEARTBEAT_URL", raising=False)
    monkeypatch.setenv("PDP_FITBIT_DAILY_ENABLED", "true")
    monkeypatch.setattr(job, "connect", lambda config: None)
    monkeypatch.setattr(job, "WarehouseConfig", SimpleNamespace(from_env=lambda: None))
    monkeypatch.setattr(job, "Warehouse", lambda connection: warehouse)
    monkeypatch.setattr(job, "validate_runtime_policy", lambda source: None)
    source = get_source("screen_time", "app-usage")
    warehouse.migrate()
    for relation in source.required_relations:
        if relation != "base.screen_time_event":
            warehouse.connection.execute(f"CREATE OR REPLACE VIEW {relation} AS SELECT 1 AS value")
    monkeypatch.setattr(job, "get_sources", lambda *args, **kwargs: (source,))
    calls = []

    def repository():
        from datetime import UTC, datetime

        calls.append("screen_time")
        if screen_time_failed:
            raise OSError("source unavailable")
        return SimpleNamespace(
            list_raw=lambda prefix: [],
            get_device_manifest=lambda: CollectorDeviceManifest(
                (), datetime.now(UTC), stream="app-usage"
            ),
            list_scan_receipts=list,
        )

    def daily(*, warehouse):
        assert calls == ["screen_time"] and not closed
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM ops.job_lock WHERE job_name='reconciliation'"
            )
            == 0
        )
        calls.append("fitbit")
        return DailySummary()

    monkeypatch.setattr(source, "repository_from_env", repository)
    monkeypatch.setattr(runtime, "run_daily_from_env", daily, raising=False)
    try:
        assert job.run_reconciliation_from_env(all_streams=True) == int(screen_time_failed)
        assert calls == ["screen_time", "fitbit"] and closed == [True]
    finally:
        original_close()
