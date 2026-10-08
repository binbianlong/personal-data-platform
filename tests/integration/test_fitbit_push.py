import base64
import json
import logging
from contextlib import closing
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from personal_data_platform.sources.fitbit.models import Notification
from tests.integration.test_fitbit_acquisition import NOW, WINDOW, direct_setup

SUBSCRIPTION = "projects/test/subscriptions/pdp-fitbit-west-pull"


def envelope(subject="self", subscription=SUBSCRIPTION):
    notification = Notification("notice", subject, (WINDOW,), NOW)
    body = json.dumps({"schema_version": 1, **asdict(notification)}, default=str).encode()
    return {
        "message": {"data": base64.b64encode(body).decode(), "messageId": "message"},
        "subscription": subscription,
    }


def push_client(runner):
    from personal_data_platform.sources.fitbit.service import create_worker_app

    return TestClient(create_worker_app(runner=runner, subscription=SUBSCRIPTION))


def test_push_commits_before_success_and_redelivery_does_not_duplicate(tmp_path):
    runner, api, factory = direct_setup(tmp_path)
    client = push_client(runner)
    for _ in range(2):
        assert client.post("/notifications/fitbit", json=envelope()).status_code == 204
        with closing(factory()) as warehouse:
            assert warehouse.query_rows("SELECT value FROM base.fitbit_activity_interval") == [
                (10,)
            ]
            assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 1
            assert warehouse.query_value("SELECT count(*) FROM ops.ingestion_metadata") == 0
            assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    assert api.calls == [WINDOW, WINDOW]


def test_push_failed_api_keeps_existing_data_and_retry_refetches(tmp_path, monkeypatch):
    runner, api, factory = direct_setup(tmp_path, states=(10, 20))
    client = push_client(runner)
    assert client.post("/notifications/fitbit", json=envelope()).status_code == 204
    fetch = api.fetch_captured
    monkeypatch.setattr(
        api,
        "fetch_captured",
        lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("API unavailable")),
    )
    assert client.post("/notifications/fitbit", json=envelope()).status_code == 503
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_activity_interval") == 10
    monkeypatch.setattr(api, "fetch_captured", fetch)
    assert client.post("/notifications/fitbit", json=envelope()).status_code == 204
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT value FROM base.fitbit_activity_interval") == 20


def test_push_busy_preserves_owner_and_expired_lease_can_retry(tmp_path):
    runner, api, factory = direct_setup(tmp_path)
    with closing(factory()) as warehouse:
        warehouse.migrate()
        warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    client = push_client(runner)
    assert client.post("/notifications/fitbit", json=envelope()).status_code == 503
    assert api.calls == []
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT owner_id FROM ops.job_lock") == "daily"
        warehouse.connection.execute(
            "UPDATE ops.job_lock SET expires_at=current_timestamp - INTERVAL '1 second'"
        )
    assert client.post("/notifications/fitbit", json=envelope()).status_code == 204


def test_push_paused_does_not_open_warehouse(tmp_path):
    runner, api, _ = direct_setup(tmp_path)
    runner.paused = True
    runner.warehouse_factory = lambda: pytest.fail("paused worker must not open warehouse")
    assert push_client(runner).post("/notifications/fitbit", json=envelope()).status_code == 503
    assert api.calls == []


def test_push_unknown_commit_returns_retry_and_redelivery_keeps_one_row(tmp_path, caplog):
    runner, api, factory = direct_setup(tmp_path)

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
    client = push_client(runner)
    assert client.post("/notifications/fitbit", json=envelope()).status_code == 503
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    with closing(factory()) as warehouse:
        assert warehouse.query_rows("SELECT value FROM base.fitbit_activity_interval") == [(10,)]
        warehouse.connection.execute(
            "UPDATE ops.job_lock SET expires_at=current_timestamp - INTERVAL '1 second'"
        )
    runner.warehouse_factory = factory
    assert client.post("/notifications/fitbit", json=envelope()).status_code == 204
    assert api.calls == [WINDOW, WINDOW]
    with closing(factory()) as warehouse:
        assert warehouse.query_rows("SELECT value FROM base.fitbit_activity_interval") == [(10,)]


@pytest.mark.parametrize(
    "body",
    [
        envelope(subject="other"),
        envelope(subscription="projects/other/subscriptions/wrong"),
        {},
        {"message": {"data": "!not-base64"}, "subscription": SUBSCRIPTION},
    ],
)
def test_push_invalid_notification_is_not_acknowledged_or_written(tmp_path, body):
    runner, _, _ = direct_setup(tmp_path)
    runner.warehouse_factory = lambda: pytest.fail("invalid notification must not open warehouse")
    assert push_client(runner).post("/notifications/fitbit", json=body).status_code == 400


def test_push_deadline_keeps_unfinished_scope_for_redelivery(tmp_path):
    runner, api, factory = direct_setup(tmp_path)
    elapsed = [0.0]
    runner.monotonic = lambda: elapsed[0]
    original = api.fetch_captured

    def slow(*args, **kwargs):
        value = original(*args, **kwargs)
        elapsed[0] += 481
        return value

    api.fetch_captured = slow
    assert push_client(runner).post("/notifications/fitbit", json=envelope()).status_code == 503
    with closing(factory()) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM base.fitbit_activity_interval") == 0
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 0
    api.fetch_captured = original
    runner.monotonic = lambda: 0.0
    assert push_client(runner).post("/notifications/fitbit", json=envelope()).status_code == 204
