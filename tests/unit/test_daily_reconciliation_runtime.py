from types import SimpleNamespace

import duckdb
import pytest

from personal_data_platform.reconciliation import job
from personal_data_platform.sources.fitbit import runtime
from personal_data_platform.sources.fitbit.daily import DailySummary
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
    source = SimpleNamespace(
        source_id="screen_time", stream="app-usage", repository_from_env=object
    )
    monkeypatch.setattr(job, "get_sources", lambda *args, **kwargs: (source,))
    calls = []

    def reconcile(repository, current, **kwargs):
        assert current is warehouse
        calls.append("screen_time")
        if screen_time_failed:
            raise OSError("source unavailable")
        return SimpleNamespace(
            ok=True,
            status="succeeded",
            raw_object_count=0,
            loaded_object_count=0,
            missing_object_count=0,
            failed_object_count=0,
            orphaned_loaded_object_count=0,
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

    monkeypatch.setattr(job, "run_reconciliation", reconcile)
    monkeypatch.setattr(runtime, "run_daily_from_env", daily, raising=False)
    try:
        assert job.run_reconciliation_from_env(all_streams=True) == int(screen_time_failed)
        assert calls == ["screen_time", "fitbit"] and closed == [True]
    finally:
        original_close()
