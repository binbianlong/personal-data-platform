"""The persistence boundary accepts data without executing decoder callbacks."""

import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window
from personal_data_platform.sources.iphone_screen_time.batch import (
    IPhoneScreenTimeBatch as ScreenTimeBatch,
)
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import _raw, _record


def test_raw_batch_cannot_take_control_of_database_execution():
    class DataBatch(ScreenTimeBatch):
        def write(self, *args, **kwargs):
            raise AssertionError("decoder callback executed")

    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        assert warehouse.load_object(_raw(), byte_size=1, batch=DataBatch([_record(_raw())])) == 1
        assert warehouse.query_value("SELECT count(*) FROM base.iphone_screen_time_event") == 1
        assert warehouse.query_value("SELECT status FROM ops.ingestion_metadata") == "succeeded"


def test_fitbit_commit_rolls_back_if_deadline_expires_after_writing(monkeypatch):
    from personal_data_platform.sources.fitbit.motherduck import FitbitBatch

    now = datetime(2026, 10, 30, tzinfo=UTC)
    window = Window("steps", now, now + timedelta(days=1))
    snapshot = Snapshot(
        "self", window, now, (Record("steps", "steps-1", now, now, now + timedelta(minutes=1), 10),)
    )
    clock = [100.0]
    write = FitbitBatch.write_snapshot

    def expire_after_write(*args, **kwargs):
        result = write(*args, **kwargs)
        clock[0] = 111.0
        return result

    monkeypatch.setattr(FitbitBatch, "write_snapshot", expire_after_write)
    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        assert warehouse.acquire_job_lock("loader", "owner", lease_seconds=60)
        with pytest.raises(TimeoutError):
            warehouse.commit_fitbit_snapshot(
                snapshot,
                source_key="fitbit-api:test",
                loaded_at=now,
                owner_id="owner",
                deadline=110.0,
                monotonic=lambda: clock[0],
            )
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_activity_interval") == 0
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 0


@pytest.mark.parametrize("stage", ["COMMIT", "ROLLBACK"])
def test_uncertain_daily_completion_prevents_connection_reuse(stage, monkeypatch):
    from personal_data_platform.storage.warehouse import WarehouseConnectionError

    now = datetime.now(UTC)
    with closing(Warehouse(connect(WarehouseConfig(":memory:")))) as warehouse:
        warehouse.migrate()
        warehouse.acquire_job_lock("loader", "daily", lease_seconds=60)
        warehouse.begin_job("daily-reconciliation", "daily")
        connection = warehouse.connection

        class Uncertain:
            def execute(self, sql, *args):
                if sql == stage:
                    raise RuntimeError("connection lost")
                return connection.execute(sql, *args)

        if stage == "ROLLBACK":
            monkeypatch.setattr(
                warehouse,
                "finish_job",
                lambda *a, **kw: (_ for _ in ()).throw(ValueError("write failed")),
            )
        warehouse.connection = Uncertain()
        try:
            with pytest.raises(WarehouseConnectionError):
                warehouse.complete_daily_job(
                    "daily", details={}, now=now, deadline=time.monotonic() + 10
                )
            assert not warehouse.connection_usable
            warehouse.release_job_lock("loader", "daily")
        finally:
            warehouse.connection = connection
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
        assert warehouse.query_value("SELECT status FROM ops.job_run") == "running"
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "daily"
