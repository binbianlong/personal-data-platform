import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.sources.fitbit import runtime
from personal_data_platform.sources.fitbit.models import Window
from tests.integration.test_fitbit_acquisition import setup

NOW = datetime(2026, 9, 28, 3, tzinfo=UTC)


@pytest.fixture
def manual_env(monkeypatch, tmp_path):
    runner, store, api, factory, _ = setup(tmp_path, states=("A",))
    fetch = api.fetch_captured

    def daily_records(window, **kwargs):
        captured = fetch(window, **kwargs)
        return replace(
            captured,
            snapshot=replace(
                captured.snapshot,
                records=tuple(
                    replace(record, record_id=window.start.isoformat())
                    for record in captured.snapshot.records
                ),
            ),
        )

    monkeypatch.setattr(api, "fetch_captured", daily_records)
    monkeypatch.setenv("PDP_FITBIT_SUBJECT_KEY", "self")
    monkeypatch.setenv("PDP_FITBIT_PROCESSING_PAUSED", "false")
    monkeypatch.setattr(runtime, "_warehouse", factory)
    monkeypatch.setattr(runtime, "_acquisition_runner", lambda: runner)
    logger = logging.getLogger("personal_data_platform.sources.fitbit")
    previous = (logger.level, logger.propagate, logger.handlers[:])
    yield SimpleNamespace(runner=runner, store=store, api=api, warehouse=factory)
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
    fetch = manual_env.api.fetch_captured

    def partial(window, **kwargs):
        if window.start == failed_day:
            raise TimeoutError("temporary API failure")
        return fetch(window, **kwargs)

    monkeypatch.setattr(manual_env.api, "fetch_captured", partial)
    assert runtime.run_sync_from_env(start=start, end=end, data_types=("steps",)) == 1
    first = json.loads(capsys.readouterr().out)
    assert first["completed_scopes"] == 2 and first["failed_scopes"] == 1
    assert first["first_incomplete"]["start"] == str(failed_day)
    warehouse = manual_env.warehouse()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 2
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_steps") > 0
    warehouse.close()

    monkeypatch.setattr(manual_env.api, "fetch_captured", fetch)
    assert runtime.run_sync_from_env(start=start, end=end, data_types=("steps",)) == 0
    retry = json.loads(capsys.readouterr().out)
    assert retry["completed_scopes"] == 3 and retry["first_incomplete"] is None
    warehouse = manual_env.warehouse()
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 3
    warehouse.close()
    saved = manual_env.store.puts
    assert runtime.run_sync_from_env(start=start, end=end, data_types=("steps",)) == 0
    assert manual_env.store.puts == saved


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
        assert manual_env.api.calls == [] and manual_env.store.puts == 0
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
