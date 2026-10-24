import json
from contextlib import closing
from datetime import timedelta

import pytest

from personal_data_platform.sources.fitbit.models import (
    HeartRateMinute,
    HeartRateMinuteSnapshot,
    Notification,
    Window,
)
from personal_data_platform.sources.fitbit.writer import FitbitMinuteBatch
from tests.integration.fitbit.helpers import drain_notification
from tests.integration.fitbit.test_fitbit_acquisition import NOW, direct_setup
from tests.unit.fitbit.test_fitbit_api import FakeTransport, client, rollup
from tests.unit.fitbit.test_fitbit_batch import Subscriber


def notice(window, *, mode="bounded_minutes"):
    return Notification("heart-notice", "self", (window,), NOW, acquisition_mode=mode)


def test_waits_for_tail_before_api_and_warehouse_and_retry_completes(tmp_path, caplog):
    runner, _, factory = direct_setup(tmp_path)
    subscriber = Subscriber()
    now = [NOW + timedelta(minutes=1, seconds=30)]
    runner.clock = lambda: now[0]
    transport = FakeTransport(
        [{"rollupDataPoints": [rollup(NOW), rollup(NOW + timedelta(minutes=1))]}]
    )
    runner.client = client(transport, clock=lambda: now[0])
    opened = []

    def tracked_factory():
        opened.append(True)
        return factory()

    runner.warehouse_factory = tracked_factory
    window = Window("heart-rate", NOW, NOW + timedelta(minutes=2))
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    assert not drain_notification(runner, notice(window), subscriber=subscriber).ok
    assert opened == [] and not subscriber.acks
    assert transport.calls == []
    now[0] = NOW + timedelta(minutes=2, seconds=1)
    assert drain_notification(runner, notice(window), subscriber=subscriber).ok
    assert len(opened) == len(transport.calls) == 1
    assert subscriber.acks == ["secret-ack-notice"]
    assert json.loads(transport.calls[0][3])["range"] == {
        "startTime": "2026-10-09T15:00:00Z",
        "endTime": "2026-10-09T15:02:00Z",
    }
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_heart_rate_minute") == 2
    scopes = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert [r.status for r in scopes] == ["succeeded"]
    assert scopes[0].summary["acquisition_mode"] == "bounded_minutes"
    attempts = [
        r.summary
        for r in caplog.records
        if getattr(r, "event", None) == "fitbit_notification_processed"
    ]
    assert attempts[0]["pubsub_message_id"] == attempts[1]["pubsub_message_id"]
    assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]


def test_shortened_api_range_is_not_committed_or_acknowledged(tmp_path, caplog):
    runner, _, factory = direct_setup(tmp_path)
    subscriber = Subscriber()
    runner.clock = lambda: NOW + timedelta(minutes=3)
    runner.client = client(
        FakeTransport([{"rollupDataPoints": [rollup(NOW)]}]),
        clock=lambda: NOW + timedelta(minutes=1, seconds=30),
    )
    window = Window("heart-rate", NOW, NOW + timedelta(minutes=2))
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    assert not drain_notification(runner, notice(window), subscriber=subscriber).ok
    assert not subscriber.acks
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_heart_rate_minute") == 0
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 0
    scopes = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(scopes) == 1
    assert scopes[0].status == "deferred"
    assert scopes[0].summary["submitted_rows"] is None


@pytest.mark.parametrize("state", ["changed", "empty", "stale"])
def test_replaces_only_target_minutes_and_preserves_newer_data(tmp_path, caplog, state):
    runner, _, factory = direct_setup(tmp_path)
    subscriber = Subscriber()
    wide = Window("heart-rate", NOW, NOW + timedelta(minutes=3))
    rows = tuple(
        HeartRateMinute(NOW + timedelta(minutes=i), NOW + timedelta(minutes=i + 1), 60, 50, 70)
        for i in range(3)
    )
    with closing(factory()) as warehouse:
        warehouse.migrate()
        warehouse.connection.execute("BEGIN")
        FitbitMinuteBatch(
            HeartRateMinuteSnapshot("self", wide, NOW + timedelta(minutes=10), rows)
        ).write_snapshot(
            warehouse.connection, source_key="seed", loaded_at=NOW + timedelta(minutes=10)
        )
        warehouse.connection.execute("COMMIT")
    fetched = NOW + timedelta(minutes=4 if state == "stale" else 11)
    runner.clock = lambda: NOW + timedelta(minutes=11)
    points = (
        []
        if state == "empty"
        else [rollup(NOW + timedelta(minutes=1), average=80, minimum=70, maximum=90)]
    )
    transport = FakeTransport([{"rollupDataPoints": points}])
    runner.client = client(transport, clock=lambda: fetched)
    window = Window("heart-rate", NOW + timedelta(minutes=1), NOW + timedelta(minutes=2))
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    assert drain_notification(runner, notice(window), subscriber=subscriber).ok
    expected = (
        [(NOW, 60), (NOW + timedelta(minutes=2), 60)]
        if state == "empty"
        else [
            (NOW, 60),
            (NOW + timedelta(minutes=1), 60 if state == "stale" else 80),
            (NOW + timedelta(minutes=2), 60),
        ]
    )
    with closing(factory()) as warehouse:
        assert (
            warehouse.query_rows(
                "SELECT start_at,average FROM base.fitbit_heart_rate_minute ORDER BY start_at"
            )
            == expected
        )
    assert json.loads(transport.calls[0][3])["range"] == {
        "startTime": "2026-10-09T15:01:00Z",
        "endTime": "2026-10-09T15:02:00Z",
    }
    scope = next(r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition")
    assert scope.summary["submitted_rows"] == (1 if state == "changed" else 0)
    assert scope.summary["outcome"] == ("stale" if state == "stale" else "range_replaced")
    assert (
        scope.summary["requested_start"]
        == scope.summary["acquired_start"]
        == window.start.isoformat()
    )
    assert scope.summary["requested_end"] == scope.summary["acquired_end"] == window.end.isoformat()


def test_legacy_notification_still_expands_to_tokyo_day(tmp_path):
    runner, _, _ = direct_setup(tmp_path)
    subscriber = Subscriber()
    transport = FakeTransport([{"rollupDataPoints": [rollup(NOW + timedelta(minutes=10))]}])
    runner.client = client(transport, clock=lambda: NOW + timedelta(hours=1))
    window = Window("heart-rate", NOW + timedelta(minutes=10), NOW + timedelta(minutes=11))
    assert drain_notification(runner, notice(window, mode="legacy"), subscriber=subscriber).ok
    assert json.loads(transport.calls[0][3])["range"] == {
        "startTime": "2026-10-09T15:00:00Z",
        "endTime": "2026-10-09T16:00:00Z",
    }


@pytest.mark.parametrize("failure", ["api", "write"])
def test_api_or_write_failure_does_not_commit(tmp_path, monkeypatch, caplog, failure):
    runner, _, factory = direct_setup(tmp_path)
    subscriber = Subscriber()
    response = (
        TimeoutError("unavailable") if failure == "api" else {"rollupDataPoints": [rollup(NOW)]}
    )
    runner.client = client(FakeTransport([response]), clock=lambda: NOW + timedelta(minutes=2))
    if failure == "write":
        original = FitbitMinuteBatch.write_snapshot

        def fail_after_write(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError("write failed")

        monkeypatch.setattr(FitbitMinuteBatch, "write_snapshot", fail_after_write)
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    window = Window("heart-rate", NOW, NOW + timedelta(minutes=1))
    assert not drain_notification(runner, notice(window), subscriber=subscriber).ok
    assert not subscriber.acks
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_heart_rate_minute") == 0
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 0
    scopes = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(scopes) == 1
    assert scopes[0].status == "failed"
    assert scopes[0].summary["submitted_rows"] is None


def test_uncertain_commit_retries_and_redelivery_does_not_duplicate_minutes(tmp_path, caplog):
    runner, _, factory = direct_setup(tmp_path)
    subscriber = Subscriber()
    runner.client = client(
        FakeTransport([{"rollupDataPoints": [rollup(NOW)]}] * 2),
        clock=lambda: NOW + timedelta(minutes=2),
    )

    def uncertain_factory():
        warehouse = factory()
        warehouse.migrate()
        connection = warehouse.connection

        class Connection:
            commits = 0

            def execute(self, sql, *args, **kwargs):
                result = connection.execute(sql, *args, **kwargs)
                if sql == "COMMIT":
                    self.commits += 1
                    if self.commits == 2:
                        raise RuntimeError("committed but response lost")
                return result

            def __getattr__(self, name):
                return getattr(connection, name)

        warehouse.connection = Connection()
        return warehouse

    runner.warehouse_factory = uncertain_factory
    window = Window("heart-rate", NOW, NOW + timedelta(minutes=1))
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    assert not drain_notification(runner, notice(window), subscriber=subscriber).ok
    scope = next(r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition")
    assert not subscriber.acks
    assert scope.status == "unknown"
    assert scope.summary["submitted_rows"] is None
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_heart_rate_minute") == 1
        warehouse.connection.execute(
            "UPDATE ops.job_lock SET expires_at=current_timestamp - INTERVAL '1 second'"
        )
    runner.warehouse_factory = factory
    assert drain_notification(runner, notice(window), subscriber=subscriber).ok
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_heart_rate_minute") == 1
