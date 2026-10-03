from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit import runtime
from personal_data_platform.sources.fitbit.daily import DailySummary
from personal_data_platform.storage.motherduck import Warehouse


@pytest.fixture
def setup(monkeypatch):
    warehouse = Warehouse(duckdb.connect(":memory:"))
    warehouse.migrate()
    monkeypatch.setenv("PDP_FITBIT_SUBJECT_KEY", "self")
    monkeypatch.setenv("PDP_FITBIT_PROCESSING_PAUSED", "false")
    monkeypatch.setattr(runtime, "_repository", lambda: object(), raising=False)
    monkeypatch.setattr(runtime, "_client", lambda: object(), raising=False)
    yield warehouse
    warehouse.close()


def test_daily_reuses_borrowed_warehouse_and_only_reports_full_success(setup, monkeypatch):
    def collect(repository, warehouse, **kwargs):
        assert warehouse is setup and kwargs["subject_key"] == "self"
        return DailySummary(status="deferred")

    monkeypatch.setattr(runtime, "collect_daily", collect, raising=False)
    monkeypatch.setattr(
        setup, "migrate", lambda: pytest.fail("borrowed warehouse must not migrate")
    )
    result = runtime.run_daily_from_env(warehouse=setup)
    assert result.status == "deferred" and result.full_success_age_seconds >= 0
    assert (
        setup.query_value(
            "SELECT count(*) FROM ops.heartbeat WHERE monitor_name='fitbit_daily_pass'"
        )
        == 0
    )


def test_paused_daily_needs_no_secrets_or_storage(monkeypatch):
    monkeypatch.setenv("PDP_FITBIT_PROCESSING_PAUSED", "true")
    monkeypatch.setattr(runtime, "_warehouse", lambda: pytest.fail("paused must not connect"))
    assert runtime.run_daily_from_env().status == "paused"


def test_daily_failure_and_deferred_runs_do_not_reset_success_age(setup, monkeypatch):
    old = datetime.now(UTC) - timedelta(days=3)
    setup.connection.execute(
        "INSERT INTO ops.heartbeat VALUES ('fitbit_daily_pass', ?, 'old', '{}')", [old]
    )

    def fail(*args, **kwargs):
        raise OSError("API interrupted")

    monkeypatch.setattr(runtime, "collect_daily", fail, raising=False)
    with pytest.raises(OSError):
        runtime.run_daily_from_env(warehouse=setup)
    monkeypatch.setattr(runtime, "collect_daily", lambda *a, **kw: DailySummary(status="deferred"))
    result = runtime.run_daily_from_env(warehouse=setup)
    assert result.full_success_age_seconds >= 3 * 86400
    assert (
        setup.query_value(
            "SELECT succeeded_at FROM ops.heartbeat WHERE monitor_name='fitbit_daily_pass'"
        )
        == old
    )
    monkeypatch.setattr(runtime, "collect_daily", lambda *a, **kw: DailySummary())
    assert runtime.run_daily_from_env(warehouse=setup).full_success_age_seconds < 1


def test_first_failure_keeps_the_original_monitoring_baseline(setup, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("first acquisition failed")

    monkeypatch.setattr(runtime, "collect_daily", fail, raising=False)
    with pytest.raises(OSError):
        runtime.run_daily_from_env(warehouse=setup)
    first = setup.query_value(
        "SELECT succeeded_at FROM ops.heartbeat WHERE monitor_name='fitbit_daily_started'"
    )
    assert first is not None
    with pytest.raises(OSError):
        runtime.run_daily_from_env(warehouse=setup)
    assert (
        setup.query_value(
            "SELECT succeeded_at FROM ops.heartbeat WHERE monitor_name='fitbit_daily_started'"
        )
        == first
    )
    assert (
        setup.query_value(
            "SELECT count(*) FROM ops.heartbeat WHERE monitor_name='fitbit_daily_pass'"
        )
        == 0
    )
