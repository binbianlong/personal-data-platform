import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.sources.fitbit import runtime
from personal_data_platform.sources.fitbit.models import (
    DATE_TYPES,
    HeartRateMinuteSnapshot,
    Notification,
    Window,
)
from tests.integration.fitbit.test_fitbit_acquisition import direct_setup

NOW = datetime(2026, 9, 28, 3, tzinfo=UTC)


@pytest.fixture
def manual_env(monkeypatch, tmp_path):
    runner, api, factory = direct_setup(tmp_path, states=(10,))
    warehouse = factory()
    warehouse.migrate()
    warehouse.close()
    fetch = api.fetch_snapshot

    def daily_records(window, **kwargs):
        captured = fetch(window, **kwargs)
        return replace(
            captured,
            records=tuple(
                replace(record, record_id=window.start.isoformat()) for record in captured.records
            ),
        )

    monkeypatch.setattr(api, "fetch_snapshot", daily_records)
    monkeypatch.setenv("PDP_FITBIT_SUBJECT_KEY", "self")
    monkeypatch.setenv("PDP_FITBIT_PROCESSING_PAUSED", "false")
    monkeypatch.setattr(runtime, "_warehouse", factory)
    monkeypatch.setattr(runtime, "_acquisition_runner", lambda: runner)
    logger = logging.getLogger("personal_data_platform.sources.fitbit")
    previous = (logger.level, logger.propagate, logger.handlers[:])
    yield SimpleNamespace(runner=runner, api=api, warehouse=factory)
    for handler in logger.handlers[:]:
        if handler not in previous[2]:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(previous[0])
    logger.propagate = previous[1]


def test_manual_retry_records_first_unfinished_day_and_preserves_committed_data(
    manual_env, monkeypatch, capsys
):
    start = datetime(2026, 10, 1, 15, tzinfo=UTC)
    end = start + timedelta(days=3)
    failed_day = start + timedelta(days=1)
    fetch = manual_env.api.fetch_snapshot

    def partial(window, **kwargs):
        if window.start == failed_day:
            raise TimeoutError("temporary API failure")
        return fetch(window, **kwargs)

    monkeypatch.setattr(manual_env.api, "fetch_snapshot", partial)
    assert runtime.run_sync_from_env(start=start, end=end, data_types=("steps",)) == 1
    first = json.loads(capsys.readouterr().out)
    assert first["completed_scopes"] == 2 and first["failed_scopes"] == 1
    assert first["first_incomplete"]["start"] == str(failed_day)
    warehouse = manual_env.warehouse()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 2
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_activity_interval") > 0
    assert warehouse.query_value("SELECT count(*) FROM ops.job_run") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    warehouse.close()

    monkeypatch.setattr(manual_env.api, "fetch_snapshot", fetch)
    assert runtime.run_sync_from_env(start=start, end=end, data_types=("steps",)) == 0
    retry = json.loads(capsys.readouterr().out)
    assert retry["completed_scopes"] == 3 and retry["first_incomplete"] is None
    warehouse = manual_env.warehouse()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 3
    assert warehouse.query_value("SELECT count(*) FROM ops.job_run") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    warehouse.close()
    assert runtime.run_sync_from_env(start=start, end=end, data_types=("steps",)) == 0
    warehouse = manual_env.warehouse()
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM ops.ingestion_metadata WHERE source_id='fitbit'"
        )
        == 0
    )
    warehouse.close()


def test_manual_old_narrow_range_keeps_exact_bounds(manual_env, capsys):
    start = datetime(2020, 1, 2, 1, 23, tzinfo=UTC)
    end = start + timedelta(minutes=2)
    assert runtime.run_sync_from_env(start=start, end=end, data_types=("steps",)) == 0
    assert manual_env.api.calls == [Window("steps", start, end)]
    warehouse = manual_env.warehouse()
    assert warehouse.query_rows("SELECT range_start,range_end FROM ops.fitbit_coverage") == [
        (start, end)
    ]
    assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.job_run") == 0
    warehouse.close()


def test_daily_paused_leaves_existing_data_and_shared_owner(manual_env, monkeypatch):
    monkeypatch.setenv("PDP_FITBIT_PROCESSING_PAUSED", "true")
    warehouse = manual_env.warehouse()
    assert warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    try:
        result = runtime.run_daily_repair(
            now=NOW, warehouse=warehouse, lease_owner="daily", timeout_seconds=6000
        )
        assert not result.ok and result.deferred_scopes == 1
        assert manual_env.api.calls == []
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "daily"
    finally:
        warehouse.close()


def test_manual_busy_does_not_release_competing_owner(manual_env):
    from personal_data_platform.loader.job import JobAlreadyRunning

    warehouse = manual_env.warehouse()
    assert warehouse.acquire_job_lock("loader", "other", lease_seconds=7500)
    warehouse.close()
    with pytest.raises(JobAlreadyRunning):
        runtime.run_sync_from_env(start=NOW, end=NOW + timedelta(hours=1), data_types=("steps",))
    assert manual_env.api.calls == []
    warehouse = manual_env.warehouse()
    assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "other"
    warehouse.close()


def test_daily_repair_fetches_ten_units_using_shared_warehouse_and_owner(tmp_path, monkeypatch):
    logger = logging.getLogger("personal_data_platform.sources.fitbit")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logger, "propagate", logger.propagate)
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    runner, api, factory = direct_setup(tmp_path, states=(None,))
    runner.clock = lambda: now

    def heart_rate(window, *, subject_key):
        api.calls.append(window)
        return HeartRateMinuteSnapshot(subject_key, window, now, ())

    monkeypatch.setattr(api, "fetch_heart_rate_minutes", heart_rate, raising=False)
    monkeypatch.setattr(runtime, "_acquisition_runner", lambda: runner)
    monkeypatch.setenv("PDP_FITBIT_PROCESSING_PAUSED", "false")
    monkeypatch.setattr(
        runner, "warehouse_factory", lambda: pytest.fail("daily repair must reuse its warehouse")
    )
    warehouse = factory()
    warehouse.migrate()
    assert warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    try:
        result = runtime.run_daily_repair(
            now=now, warehouse=warehouse, lease_owner="daily", timeout_seconds=6000
        )
        assert result.ok and result.completed_scopes == 10
        assert len(api.calls) == 10 and len(set(api.calls)) == 10
        assert {window.data_type for window in api.calls} == {
            "steps",
            "heart-rate",
            "daily-resting-heart-rate",
            "active-zone-minutes",
            "sleep",
        }
        for kind in {window.data_type for window in api.calls}:
            bounds = [
                (window.start, window.end) for window in api.calls if window.data_type == kind
            ]
            if kind in DATE_TYPES:
                assert bounds == [
                    (datetime(2026, 10, 10, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC)),
                    (datetime(2026, 10, 11, tzinfo=UTC), datetime(2026, 10, 12, tzinfo=UTC)),
                ]
            else:
                assert bounds == [
                    (datetime(2026, 10, 9, 15, tzinfo=UTC), datetime(2026, 10, 10, 15, tzinfo=UTC)),
                    (
                        datetime(2026, 10, 10, 15, tzinfo=UTC),
                        datetime(2026, 10, 11, 15, tzinfo=UTC),
                    ),
                ]
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 10
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "daily"
    finally:
        warehouse.close()


@pytest.mark.parametrize("mode", ["legacy", "bounded_steps"])
def test_old_notification_still_fetches_its_historical_range(manual_env, mode):
    start = datetime(2020, 1, 2, 15, tzinfo=UTC)
    window = Window("steps", start, start + timedelta(days=1))
    notification = Notification(
        "historical", "self", (window,), start + timedelta(days=2), acquisition_mode=mode
    )
    warehouse = manual_env.warehouse()
    assert warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    try:
        result = manual_env.runner.process_notification(
            notification, warehouse=warehouse, lease_owner="daily", timeout_seconds=6000
        )
        assert result.ok and result.completed_scopes == 1
        assert manual_env.api.calls == [window]
        assert warehouse.query_rows("SELECT range_start,range_end FROM ops.fitbit_coverage") == [
            (window.start, window.end)
        ]
    finally:
        warehouse.close()
