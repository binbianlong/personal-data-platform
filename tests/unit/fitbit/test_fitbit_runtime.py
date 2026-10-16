import logging

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_logger(monkeypatch):
    logger = logging.getLogger("personal_data_platform.sources.fitbit")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logger, "propagate", logger.propagate)


def test_pubsub_receiver_uses_only_webhook_config(monkeypatch):
    import personal_data_platform.sources.fitbit.runtime as runtime

    monkeypatch.setenv("PDP_FITBIT_SUBJECT_KEY", "self")
    monkeypatch.setenv(
        "PDP_FITBIT_WEBHOOK_CONFIG", '{"authorization":"header","health_user_id":"owner"}'
    )
    monkeypatch.setenv("PDP_FITBIT_PUBSUB_TOPIC", "projects/test/topics/fitbit")
    monkeypatch.setattr(
        runtime,
        "_warehouse",
        lambda: (_ for _ in ()).throw(AssertionError("receiver may not construct Raw store")),
    )
    monkeypatch.setattr(
        runtime.GoogleOAuth,
        "from_env",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("receiver may not construct OAuth")
        ),
    )
    apps = []
    monkeypatch.setattr(runtime.uvicorn, "run", lambda app, **kwargs: apps.append(app))
    assert runtime.run_serve_from_env() == 0
    assert len(apps) == 1
    assert "/internal/tasks/fitbit" not in {route.path for route in apps[0].routes}


@pytest.mark.parametrize(
    ("now", "physical_start", "physical_end", "civil_start", "civil_end"),
    [
        (
            "2026-10-12T12:00:00Z",
            "2026-10-09T15:00:00Z",
            "2026-10-11T15:00:00Z",
            "2026-10-10T00:00:00Z",
            "2026-10-12T00:00:00Z",
        ),
        (
            "2026-10-12T14:59:59Z",
            "2026-10-09T15:00:00Z",
            "2026-10-11T15:00:00Z",
            "2026-10-10T00:00:00Z",
            "2026-10-12T00:00:00Z",
        ),
        (
            "2026-10-12T15:00:00Z",
            "2026-10-10T15:00:00Z",
            "2026-10-12T15:00:00Z",
            "2026-10-11T00:00:00Z",
            "2026-10-13T00:00:00Z",
        ),
    ],
)
def test_daily_repair_uses_two_completed_tokyo_days_without_device_cursor(
    monkeypatch, now, physical_start, physical_end, civil_start, civil_end
):
    from datetime import datetime

    import duckdb

    from personal_data_platform.sources.fitbit import runtime
    from personal_data_platform.sources.fitbit.acquisition import AcquisitionSummary
    from personal_data_platform.sources.fitbit.models import DATE_TYPES
    from personal_data_platform.storage.motherduck import Warehouse

    warehouse = Warehouse(duckdb.connect())
    warehouse.migrate()
    warehouse.acquire_job_lock("loader", "daily", lease_seconds=7500)
    calls = []

    class Runner:
        subject_key = "self"

        def run_windows(self, windows, **kwargs):
            calls.append((windows, kwargs))
            return AcquisitionSummary(completed_scopes=10)

    monkeypatch.setattr(runtime, "_acquisition_runner", lambda: Runner())
    monkeypatch.delenv("PDP_FITBIT_PROCESSING_PAUSED", raising=False)
    try:
        result = runtime.run_daily_repair(
            now=datetime.fromisoformat(now),
            warehouse=warehouse,
            lease_owner="daily",
            timeout_seconds=100,
        )
        assert result.ok and len(calls) == 1
        windows, kwargs = calls[0]
        assert len(windows) == 5
        for window in windows:
            start, end = (
                (civil_start, civil_end)
                if window.data_type in DATE_TYPES
                else (physical_start, physical_end)
            )
            assert window.start == datetime.fromisoformat(start)
            assert window.end == datetime.fromisoformat(end)
        assert kwargs["warehouse"] is warehouse and kwargs["lease_owner"] == "daily"
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='fitbit_repair_cursor'"
            )
            == 0
        )
    finally:
        warehouse.close()


def test_worker_composes_regional_pull_client_and_keeps_push_route(monkeypatch):
    import time
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from google.cloud import pubsub_v1

    from personal_data_platform.sources.fitbit import runtime

    runner = SimpleNamespace(
        subject_key="self",
        paused=False,
        monotonic=time.monotonic,
        clock=lambda: datetime.now(UTC),
        warehouse_factory=lambda: pytest.fail("empty queue must not open a database"),
    )
    calls = []

    class Subscriber:
        closed = False

        def pull(self, *, request, retry, timeout):
            assert request["subscription"] == "projects/test/subscriptions/fitbit"
            assert retry is None and timeout <= 2
            return SimpleNamespace(received_messages=[])

        def close(self):
            self.closed = True

    subscriber = Subscriber()

    def construct(**kwargs):
        calls.append(kwargs)
        return subscriber

    monkeypatch.setenv("PDP_FITBIT_PUBSUB_SUBSCRIPTION", "projects/test/subscriptions/fitbit")
    monkeypatch.setattr(pubsub_v1, "SubscriberClient", construct)
    monkeypatch.setattr(runtime, "_acquisition_runner", lambda: runner)

    def serve(app, **kwargs):
        assert "/notifications/fitbit" in {route.path for route in app.routes}
        response = TestClient(app).post("/notifications/fitbit/drain")
        assert response.status_code == 200 and response.json()["session_opens"] == 0

    monkeypatch.setattr(runtime.uvicorn, "run", serve)
    assert runtime.run_worker_from_env() == 0
    assert calls == [{"client_options": {"api_endpoint": "pubsub.us-west1.rep.googleapis.com"}}]
    assert subscriber.closed
