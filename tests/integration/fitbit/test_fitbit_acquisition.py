from contextlib import closing
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit.acquisition import AcquisitionRunner
from personal_data_platform.sources.fitbit.models import CapturedSnapshot, Record, Snapshot, Window
from personal_data_platform.sources.fitbit.writer import FitbitBatch
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConnectionError

NOW = datetime(2026, 10, 9, 15, tzinfo=UTC)
WINDOW = Window("steps", NOW - timedelta(days=1), NOW)


def direct_setup(tmp_path, states=(10, 10, 20, 10, None)):
    path = str(tmp_path / "direct.duckdb")

    def factory():
        return Warehouse(duckdb.connect(path))

    class API:
        def __init__(self):
            self.calls = []

        def fetch_captured(self, window, *, subject_key):
            self.calls.append(window)
            value = states[min(len(self.calls) - 1, len(states) - 1)]
            records = (
                ()
                if value is None
                else (
                    Record(
                        window.data_type,
                        "stable",
                        window.start,
                        window.start,
                        window.start + timedelta(minutes=1),
                        value,
                    ),
                )
            )
            return CapturedSnapshot(
                Snapshot(subject_key, window, NOW + timedelta(seconds=len(self.calls)), records),
                ({"unknown": "transient response"},),
            )

    api = API()
    runner = AcquisitionRunner(
        client=api,
        warehouse_factory=factory,
        subject_key="self",
        clock=lambda: NOW + timedelta(hours=1),
    )
    return runner, api, factory


def run_range(runner, factory, windows=(WINDOW,), timeout=100):
    with closing(factory()) as warehouse:
        warehouse.migrate()
        assert warehouse.acquire_job_lock("loader", "manual", lease_seconds=7500)
        try:
            return runner.run_windows(
                windows, warehouse=warehouse, lease_owner="manual", timeout_seconds=timeout
            )
        finally:
            if warehouse.connection_usable:
                warehouse.release_job_lock("loader", "manual")


def test_direct_acquisition_keeps_unchanged_rows_and_applies_changes_and_empty(tmp_path, caplog):
    runner, api, factory = direct_setup(tmp_path)
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    previous = None
    for expected in (10, 10, 20, 10, None):
        assert run_range(runner, factory).ok
        with closing(factory()) as warehouse:
            rows = warehouse.query_rows(
                "SELECT value,source_key,loaded_at FROM base.fitbit_activity_interval"
            )
            assert [row[0] for row in rows] == ([] if expected is None else [expected])
            if previous is not None and expected == 10 and len(api.calls) == 2:
                assert rows == previous
            previous = rows
            assert (
                warehouse.query_value(
                    "SELECT count(*) FROM ops.ingestion_metadata WHERE source_id='fitbit'"
                )
                == 0
            )
            assert warehouse.query_value("SELECT source_key FROM ops.fitbit_coverage").startswith(
                "fitbit-api:"
            )
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_deleted_record") == 1
    entries = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(entries) == 5
    assert [r.summary["outcome"] for r in entries] == [
        "range_replaced",
        "unchanged",
        "range_replaced",
        "range_replaced",
        "range_replaced",
    ]
    assert [r.summary["fetched_rows"] for r in entries] == [1, 1, 1, 1, 0]
    assert [r.summary["submitted_rows"] for r in entries] == [1, 0, 1, 1, 0]
    assert all(r.status == "succeeded" for r in entries)
    assert all(r.summary["notification_id"] is None for r in entries)
    assert "transient response" not in repr([r.summary for r in entries])


def test_direct_write_failure_rolls_back_and_retry_refetches(tmp_path, monkeypatch, caplog):
    runner, api, factory = direct_setup(tmp_path, states=(10, 20, 20))
    assert run_range(runner, factory).ok
    original = FitbitBatch.write_snapshot
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")

    def fail_after_write(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("write failed")

    monkeypatch.setattr(FitbitBatch, "write_snapshot", fail_after_write)
    assert run_range(runner, factory).failed_scopes == 1
    entries = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(entries) == 1
    assert entries[0].status == "failed"
    assert entries[0].summary["fetched_rows"] == 1
    assert entries[0].summary["submitted_rows"] is None
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_activity_interval") == 10
    monkeypatch.setattr(FitbitBatch, "write_snapshot", original)
    assert run_range(runner, factory).ok
    assert len(api.calls) == 3
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_activity_interval") == 20


@pytest.mark.parametrize("stage", ["COMMIT", "ROLLBACK"])
def test_uncertain_transaction_stops_processing(tmp_path, monkeypatch, stage, caplog):
    runner, api, factory = direct_setup(tmp_path)
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    with closing(factory()) as warehouse:
        warehouse.migrate()
        warehouse.acquire_job_lock("loader", "manual", lease_seconds=7500)
        connection = warehouse.connection

        class UncertainConnection:
            def execute(self, sql, *args, **kwargs):
                if sql == stage:
                    raise RuntimeError("connection lost")
                return connection.execute(sql, *args, **kwargs)

        if stage == "ROLLBACK":
            monkeypatch.setattr(
                FitbitBatch,
                "write_snapshot",
                lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("write failed")),
            )
        warehouse.connection = UncertainConnection()
        second = Window("steps", NOW, NOW + timedelta(days=1))
        with pytest.raises(WarehouseConnectionError):
            runner.run_windows(
                (WINDOW, second), warehouse=warehouse, lease_owner="manual", timeout_seconds=100
            )
        assert not warehouse.connection_usable
        assert api.calls == [WINDOW]
        warehouse.connection = connection
    entries = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(entries) == 1
    assert entries[0].status == "unknown"
    assert entries[0].summary["submitted_rows"] is None


def test_minute_acquisition_logs_effective_range_separately_from_requested(tmp_path, caplog):
    from personal_data_platform.sources.fitbit.models import (
        HeartRateMinute,
        HeartRateMinuteSnapshot,
    )

    runner, _, factory = direct_setup(tmp_path)
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    start = NOW + timedelta(seconds=20)
    requested = Window("heart-rate", start, NOW + timedelta(minutes=4, seconds=10))
    effective = Window("heart-rate", NOW + timedelta(minutes=1), NOW + timedelta(minutes=3))

    class Client:
        def fetch_heart_rate_minutes(self, window, *, subject_key):
            assert window == requested
            return HeartRateMinuteSnapshot(
                subject_key,
                effective,
                NOW + timedelta(minutes=3, seconds=30),
                (
                    HeartRateMinute(
                        effective.start, effective.start + timedelta(minutes=1), 60, 50, 70
                    ),
                ),
                (),
            )

    runner.client = Client()
    assert run_range(runner, factory, windows=(requested,)).ok
    entries = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(entries) == 1
    summary = entries[0].summary
    assert summary["requested_start"] == requested.start.isoformat()
    assert summary["requested_end"] == requested.end.isoformat()
    assert summary["acquired_start"] == effective.start.isoformat()
    assert summary["acquired_end"] == effective.end.isoformat()
    assert summary["fetched_rows"] == summary["submitted_rows"] == 1


def test_mismatched_api_snapshot_is_not_logged_as_a_complete_acquisition(tmp_path, caplog):
    from dataclasses import replace

    runner, api, factory = direct_setup(tmp_path)
    fetch = api.fetch_captured
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")

    def wrong_subject(window, *, subject_key):
        result = fetch(window, subject_key=subject_key)
        return replace(result, snapshot=replace(result.snapshot, subject_key="other"))

    api.fetch_captured = wrong_subject
    assert run_range(runner, factory).failed_scopes == 1
    entries = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(entries) == 1
    assert entries[0].status == "failed"
    assert entries[0].summary["fetched_rows"] is None
    assert entries[0].summary["acquired_start"] is None
