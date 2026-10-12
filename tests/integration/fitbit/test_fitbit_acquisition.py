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


def test_direct_acquisition_keeps_unchanged_rows_and_applies_changes_and_empty(tmp_path):
    runner, api, factory = direct_setup(tmp_path)
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


def test_direct_write_failure_rolls_back_and_retry_refetches(tmp_path, monkeypatch):
    runner, api, factory = direct_setup(tmp_path, states=(10, 20, 20))
    assert run_range(runner, factory).ok
    original = FitbitBatch.write_snapshot

    def fail_after_write(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("write failed")

    monkeypatch.setattr(FitbitBatch, "write_snapshot", fail_after_write)
    assert run_range(runner, factory).failed_scopes == 1
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_activity_interval") == 10
    monkeypatch.setattr(FitbitBatch, "write_snapshot", original)
    assert run_range(runner, factory).ok
    assert len(api.calls) == 3
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_activity_interval") == 20


@pytest.mark.parametrize("stage", ["COMMIT", "ROLLBACK"])
def test_uncertain_transaction_stops_processing(tmp_path, monkeypatch, stage):
    runner, api, factory = direct_setup(tmp_path)
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
