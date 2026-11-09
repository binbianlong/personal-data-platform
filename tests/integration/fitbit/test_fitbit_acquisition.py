from contextlib import closing
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.sources.fitbit.acquisition import AcquisitionRunner
from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window
from personal_data_platform.sources.fitbit.motherduck import FitbitBatch
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConnectionError
from tests.integration.fitbit.helpers import drain_notification

NOW = datetime(2026, 10, 9, 15, tzinfo=UTC)
WINDOW = Window("steps", NOW - timedelta(days=1), NOW)


def direct_setup(tmp_path, states=(10, 10, 20, 10, None)):
    path = str(tmp_path / "direct.duckdb")

    def factory():
        return Warehouse(duckdb.connect(path))

    class API:
        def __init__(self):
            self.calls = []

        def fetch_snapshot(self, window, *, subject_key):
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
            return Snapshot(subject_key, window, NOW + timedelta(seconds=len(self.calls)), records)

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


@pytest.mark.parametrize("kind", ["steps", "heart-rate"])
@pytest.mark.parametrize("takeover", [False, True])
def test_paginated_acquisition_checks_lease_per_request_and_write_boundary(
    tmp_path, kind, takeover
):
    from tests.fitbit_points import FakeTransport, client

    runner, _, factory = direct_setup(tmp_path)
    window = Window(kind, WINDOW.start, WINDOW.end)
    with closing(factory()) as warehouse, closing(factory()) as competitor:
        warehouse.migrate()
        assert warehouse.acquire_job_lock("loader", "manual", lease_seconds=7500)
        table = "steps_interval" if kind == "steps" else "heart_rate_minute"
        if kind == "steps":
            warehouse.connection.execute(
                "INSERT INTO base.steps_interval VALUES ('seed',?,?,NULL,10,NULL,NULL)",
                [window.start, window.start + timedelta(minutes=1)],
            )
        else:
            warehouse.connection.execute(
                "INSERT INTO base.heart_rate_minute VALUES ('seed',?,60,60,60,'old')",
                [window.start],
            )

        def during_request():
            # An independent connection sees the lease throughout API acquisition.
            competitor.require_job_lock("manual")
            if takeover:
                competitor.connection.execute(
                    "UPDATE ops.job_lock SET expires_at=now()-INTERVAL 1 SECOND"
                )
                assert competitor.acquire_job_lock("loader", "other", lease_seconds=7500)

        transport = FakeTransport([{"nextPageToken": "next"}, {}], before_request=during_request)
        runner.client = client(transport, clock=lambda: NOW + timedelta(hours=1))
        windows = (
            (window,)
            if not takeover
            else (window, Window(kind, WINDOW.end, WINDOW.end + timedelta(hours=1)))
        )
        result = runner.run_windows(
            windows, warehouse=warehouse, lease_owner="manual", timeout_seconds=100
        )
        assert result.completed_scopes == (0 if takeover else 1)
        assert result.deferred_scopes == (len(windows) if takeover else 0)
        assert len(transport.calls) == (1 if takeover else 2)
        # An incomplete page sequence neither deletes existing data nor reaches a later scope.
        assert warehouse.query_value(f"SELECT count(*) FROM base.{table}") == int(takeover)
        if takeover:
            competitor.require_job_lock("other")
        assert runner.client.request_guard is None


@pytest.mark.parametrize("loss", ["owner", "expiry"])
@pytest.mark.parametrize("stage", ["before_api", "before_begin", "before_commit"])
def test_lease_loss_still_stops_api_or_rolls_back_the_acquisition(
    tmp_path, monkeypatch, loss, stage
):
    from tests.fitbit_points import FakeTransport, client

    runner, _, factory = direct_setup(tmp_path)
    with closing(factory()) as warehouse:
        warehouse.migrate()
        assert warehouse.acquire_job_lock("loader", "manual", lease_seconds=7500)

        def lose():
            if loss == "owner":
                warehouse.connection.execute("UPDATE ops.job_lock SET owner_id='other'")
            else:
                warehouse.connection.execute("UPDATE ops.job_lock SET expires_at=now()")

        if stage == "before_api":
            lose()
        transport = FakeTransport([{}], before_request=lose if stage == "before_begin" else None)
        runner.client = client(transport, clock=lambda: NOW + timedelta(hours=1))
        if stage == "before_commit":
            write = FitbitBatch.write_snapshot

            def write_then_lose(*args, **kwargs):
                result = write(*args, **kwargs)
                lose()
                return result

            monkeypatch.setattr(FitbitBatch, "write_snapshot", write_then_lose)
        result = runner.run_windows(
            (WINDOW,), warehouse=warehouse, lease_owner="manual", timeout_seconds=100
        )
        assert result.deferred_scopes == 1 and result.completed_scopes == 0
        assert len(transport.calls) == (0 if stage == "before_api" else 1)
        assert "ops.fitbit_coverage" not in warehouse.relation_names()
        assert warehouse.query_value("SELECT count(*) FROM base.steps_interval") == 0


def test_direct_acquisition_keeps_unchanged_rows_and_applies_changes_and_empty(tmp_path, caplog):
    runner, api, factory = direct_setup(tmp_path)
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    previous = None
    for expected in (10, 10, 20, 10, None):
        assert run_range(runner, factory).ok
        with closing(factory()) as warehouse:
            rows = warehouse.query_rows(
                "SELECT steps_interval_id,start_at,end_at,steps FROM base.steps_interval"
            )
            assert [row[3] for row in rows] == ([] if expected is None else [expected])
            if previous is not None and expected == 10 and len(api.calls) == 2:
                assert rows == previous
            previous = rows
            assert {r for r in warehouse.relation_names() if r.startswith("ops.")} == {
                "ops.job_lock",
                "ops.schema_migration",
            }
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.steps_interval") == 0
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
        assert warehouse.query_value("SELECT steps FROM base.steps_interval") == 10
    monkeypatch.setattr(FitbitBatch, "write_snapshot", original)
    assert run_range(runner, factory).ok
    assert len(api.calls) == 3
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT steps FROM base.steps_interval") == 20


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
    fetch = api.fetch_snapshot
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")

    def wrong_subject(window, *, subject_key):
        result = fetch(window, subject_key=subject_key)
        return replace(result, subject_key="other")

    api.fetch_snapshot = wrong_subject
    assert run_range(runner, factory).failed_scopes == 1
    entries = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(entries) == 1
    assert entries[0].status == "failed"
    assert entries[0].summary["fetched_rows"] is None
    assert entries[0].summary["acquired_start"] is None


def test_schema_two_steps_acquires_only_the_notified_provider_date(tmp_path):
    from personal_data_platform.sources.fitbit.models import Notification

    runner, api, factory = direct_setup(tmp_path, states=(None,))
    date = datetime(2026, 10, 1, tzinfo=UTC)
    notice = Notification(
        "civil-steps",
        "self",
        (Window("steps", date, date + timedelta(days=1), time_basis="civil"),),
        NOW,
        acquisition_mode="bounded_steps",
    )
    assert drain_notification(runner, notice).ok
    assert api.calls == list(notice.windows)


def test_bounded_steps_partial_does_not_expand_to_a_day(tmp_path):
    from personal_data_platform.sources.fitbit.models import Notification

    runner, api, factory = direct_setup(tmp_path, states=(None,))
    window = Window("steps", NOW - timedelta(minutes=3), NOW - timedelta(minutes=1))
    assert drain_notification(
        runner, Notification("partial", "self", (window,), NOW, acquisition_mode="bounded_steps")
    ).ok
    assert api.calls == [window]


def test_bounded_steps_rejects_shortened_acquisition_without_writes(tmp_path):
    from personal_data_platform.sources.fitbit.models import Notification

    runner, api, factory = direct_setup(tmp_path, states=(None,))
    window = Window("steps", NOW - timedelta(minutes=3), NOW - timedelta(minutes=1))
    shorter = Window("steps", window.start, window.end - timedelta(minutes=1))
    api.fetch_snapshot = lambda requested, subject_key: Snapshot(subject_key, shorter, NOW, ())
    result = drain_notification(
        runner, Notification("partial", "self", (window,), NOW, acquisition_mode="bounded_steps")
    )
    assert not result.ok
    assert result.deferred == 1
    with closing(factory()) as warehouse:
        assert "ops.fitbit_coverage" not in warehouse.relation_names()


def test_borrowed_notification_uses_existing_session_and_preserves_owner(tmp_path):
    from personal_data_platform.sources.fitbit.models import Notification

    runner, api, factory = direct_setup(tmp_path)
    with closing(factory()) as warehouse:
        warehouse.migrate()
        warehouse.acquire_job_lock("loader", "batch", lease_seconds=600)
        runner.warehouse_factory = lambda: pytest.fail("must borrow session")
        result = runner.process_notification(
            Notification("borrowed", "self", (WINDOW,), NOW),
            warehouse=warehouse,
            lease_owner="batch",
            timeout_seconds=450,
        )
        assert result.ok and api.calls == [WINDOW]
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "batch"
        assert warehouse.connection_usable


def test_acquisition_timer_is_joined_before_shared_connection_reuse(tmp_path, monkeypatch):
    import personal_data_platform.sources.fitbit.acquisition as acquisition

    runner, _, factory = direct_setup(tmp_path)
    calls = []

    class Timer:
        def cancel(self):
            calls.append("cancel")

        def join(self):
            calls.append("join")

    monkeypatch.setattr(acquisition, "interrupt_after", lambda *args: Timer())
    assert run_range(runner, factory).ok
    assert calls == ["cancel", "join"]
