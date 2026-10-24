import logging
from contextlib import closing

import pytest

from personal_data_platform.sources.fitbit.models import Notification
from tests.integration.fitbit.helpers import drain_notification
from tests.integration.fitbit.test_fitbit_acquisition import NOW, WINDOW, direct_setup
from tests.unit.fitbit.test_fitbit_batch import Subscriber


def notice():
    return Notification("notice", "self", (WINDOW,), NOW)


def test_drain_commits_before_success_and_redelivery_does_not_duplicate(tmp_path, caplog):
    runner, api, factory = direct_setup(tmp_path)
    subscriber = Subscriber()
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    for _ in range(2):
        assert drain_notification(runner, notice(), subscriber=subscriber).ok
        with closing(factory()) as warehouse:
            assert warehouse.query_rows("SELECT value FROM base.fitbit_activity_interval") == [
                (10,)
            ]
            assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 1
            assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 0
            assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    assert api.calls == [WINDOW, WINDOW]
    entries = [
        r for r in caplog.records if getattr(r, "event", None) == "fitbit_notification_processed"
    ]
    assert len(entries) == 2
    assert (
        entries[0].summary["pubsub_message_id"]
        == entries[1].summary["pubsub_message_id"]
        == "message"
    )
    assert entries[0].summary["attempt_id"] != entries[1].summary["attempt_id"]
    assert entries[0].summary["completed_scopes"] == 1
    assert entries[0].summary["failed_scopes"] == 0
    assert subscriber.acks == ["secret-ack-notice", "secret-ack-notice"]
    assert entries[0].summary["deferred_scopes"] == 0
    scopes = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(scopes) == 2
    assert scopes[0].summary["notification_id"] == entries[0].summary["group_id"]
    assert entries[0].summary["notification_id"] == "notice"
    assert scopes[0].summary["attempt_id"] == entries[0].summary["attempt_id"]
    assert scopes[1].summary["attempt_id"] == entries[1].summary["attempt_id"]


def test_drain_failed_api_keeps_existing_data_and_retry_refetches(tmp_path, monkeypatch, caplog):
    runner, api, factory = direct_setup(tmp_path, states=(10, 20))
    subscriber = Subscriber()
    assert drain_notification(runner, notice(), subscriber=subscriber).ok
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    fetch = api.fetch_snapshot
    monkeypatch.setattr(
        api,
        "fetch_snapshot",
        lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("API unavailable")),
    )
    assert not drain_notification(runner, notice(), subscriber=subscriber).ok
    assert subscriber.acks == ["secret-ack-notice"]
    scopes = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert len(scopes) == 1
    assert scopes[0].status == "failed"
    assert scopes[0].summary["fetched_rows"] is None
    assert scopes[0].summary["submitted_rows"] is None
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_activity_interval") == 10
    monkeypatch.setattr(api, "fetch_snapshot", fetch)
    assert drain_notification(runner, notice(), subscriber=subscriber).ok
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_activity_interval") == 20


def test_drain_busy_preserves_owner_and_expired_lease_can_retry(tmp_path, caplog):
    runner, api, factory = direct_setup(tmp_path)
    subscriber = Subscriber()
    with closing(factory()) as warehouse:
        warehouse.migrate()
        warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    assert not drain_notification(runner, notice(), subscriber=subscriber).ok
    assert api.calls == [] and subscriber.acks == []
    entries = [
        r for r in caplog.records if getattr(r, "event", None) == "fitbit_notification_processed"
    ]
    assert len(entries) == 1
    assert entries[0].status == "retry"
    assert entries[0].summary["pubsub_message_id"] == "message"
    assert entries[0].summary["deferred_scopes"] == 1
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "daily"
        warehouse.connection.execute(
            "UPDATE ops.job_lock SET expires_at=current_timestamp - INTERVAL '1 second'"
        )
    assert drain_notification(runner, notice(), subscriber=subscriber).ok


def test_drain_paused_does_not_open_warehouse(tmp_path):
    runner, api, _ = direct_setup(tmp_path)
    subscriber = Subscriber()
    runner.paused = True
    runner.warehouse_factory = lambda: pytest.fail("paused worker must not open warehouse")
    assert not drain_notification(runner, notice(), subscriber=subscriber).ok
    assert api.calls == [] and subscriber.acks == []


def test_drain_unknown_commit_returns_retry_and_redelivery_keeps_one_row(tmp_path, caplog):
    runner, api, factory = direct_setup(tmp_path)
    subscriber = Subscriber()

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
    assert not drain_notification(runner, notice(), subscriber=subscriber).ok
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert not subscriber.acks
    with closing(factory()) as warehouse:
        assert warehouse.query_rows("SELECT value FROM base.fitbit_activity_interval") == [(10,)]
        warehouse.connection.execute(
            "UPDATE ops.job_lock SET expires_at=current_timestamp - INTERVAL '1 second'"
        )
    runner.warehouse_factory = factory
    assert drain_notification(runner, notice(), subscriber=subscriber).ok
    assert api.calls == [WINDOW, WINDOW]
    with closing(factory()) as warehouse:
        assert warehouse.query_rows("SELECT value FROM base.fitbit_activity_interval") == [(10,)]


def test_drain_deadline_keeps_unfinished_scope_for_redelivery(tmp_path):
    runner, api, factory = direct_setup(tmp_path)
    subscriber = Subscriber()
    elapsed = [0.0]
    runner.monotonic = lambda: elapsed[0]
    original = api.fetch_snapshot

    def slow(*args, **kwargs):
        value = original(*args, **kwargs)
        elapsed[0] += 481
        return value

    api.fetch_snapshot = slow
    assert not drain_notification(runner, notice(), subscriber=subscriber).ok
    assert not subscriber.acks
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_activity_interval") == 0
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 0
    api.fetch_snapshot = original
    runner.monotonic = lambda: 0.0
    assert drain_notification(runner, notice(), subscriber=subscriber).ok
