"""Real notification, API, push and DuckDB boundaries with synthetic steps."""

import base64
import json
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import duckdb
from fastapi.testclient import TestClient

from personal_data_platform.sources.fitbit.acquisition import AcquisitionRunner
from personal_data_platform.sources.fitbit.models import Snapshot
from personal_data_platform.sources.fitbit.notifications import PubSubNotifications
from personal_data_platform.sources.fitbit.service import create_pubsub_app, create_worker_app
from personal_data_platform.sources.fitbit.webhook import GoogleHealthAuthenticator
from personal_data_platform.storage.motherduck import Warehouse
from tests.integration.fitbit.test_fitbit_service import NOW
from tests.unit.fitbit.test_fitbit_api import FakeTransport, client, steps
from tests.unit.fitbit.test_fitbit_webhook import Signature, notification


def test_civil_full_partial_full_trace_exact_filters_and_changed_rows(tmp_path, caplog):
    path = str(tmp_path / "steps.duckdb")

    def factory():
        return Warehouse(duckdb.connect(path))

    a = {
        "dataPointName": "users/me/dataTypes/steps/dataPoints/a",
        **steps(start="2026-09-26T14:00:00Z", end="2026-09-26T14:01:00Z"),
    }
    b = {
        "dataPointName": "users/me/dataTypes/steps/dataPoints/b",
        **steps(start="2026-09-26T14:01:00Z", end="2026-09-26T14:02:00Z"),
    }
    changed = {
        "dataPointName": "users/me/dataTypes/steps/dataPoints/b",
        **steps("20", start="2026-09-26T14:01:00Z", end="2026-09-26T14:02:00Z"),
    }
    transport = FakeTransport(
        [{"dataPoints": points} for points in ([a, b], [changed], [a, changed])]
    )
    times = iter(NOW + timedelta(seconds=index) for index in range(3))
    runner = AcquisitionRunner(
        client=client(transport, clock=lambda: next(times)),
        warehouse_factory=factory,
        subject_key="self",
        clock=lambda: NOW + timedelta(hours=1),
    )
    subscription = "projects/test/subscriptions/fitbit"
    worker = TestClient(create_worker_app(runner=runner, subscription=subscription))

    class Publisher:
        def __init__(self):
            self.payloads = []

        def publish(self, topic, data):
            self.payloads.append(data)
            return self

        def result(self, *, timeout):
            return "message-" + str(len(self.payloads))

    publisher = Publisher()
    receiver = TestClient(
        create_pubsub_app(
            authenticator=GoogleHealthAuthenticator(
                authorization="Bearer secret",
                health_user_id="health-owner",
                subject_key="self",
                signatures=Signature(),
            ),
            notifications=PubSubNotifications(
                topic="projects/test/topics/fitbit", publisher=publisher
            ),
        )
    )
    full = notification()
    full["data"]["intervals"] = [
        {
            "civilIso8601TimeInterval": {
                "startTime": "2026-09-26T00:00:00",
                "endTime": "2026-09-27T00:00:00",
            }
        }
    ]
    partial = notification()
    partial["data"]["intervals"][0]["physicalTimeInterval"] = {
        "startTime": "2026-09-26T14:01:00Z",
        "endTime": "2026-09-26T14:02:00Z",
    }
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    for index, notice in enumerate((full, partial, full), start=1):
        response = receiver.post(
            "/webhooks/fitbit",
            content=json.dumps(notice),
            headers={
                "authorization": "Bearer secret",
                "content-type": "application/json",
                "google-health-api-signature": base64.b64encode(b"signed").decode(),
            },
        )
        assert response.status_code == 204
        assert len(publisher.payloads) == index
        payload = publisher.payloads[-1]
        assert json.loads(payload)["schema_version"] == 2
        response = worker.post(
            "/notifications/fitbit",
            json={
                "message": {
                    "data": base64.b64encode(payload).decode(),
                    "messageId": "message-" + str(index),
                },
                "subscription": subscription,
            },
        )
        assert response.status_code == 204
    queries = [parse_qs(urlsplit(call[1]).query)["filter"][0] for call in transport.calls]
    civil = (
        'steps.interval.civil_start_time >= "2026-09-26" AND '
        'steps.interval.civil_start_time < "2026-09-27"'
    )
    assert queries == [
        civil,
        'steps.interval.start_time >= "2026-09-26T14:01:00Z" AND '
        'steps.interval.start_time < "2026-09-26T14:02:00Z"',
        civil,
    ]
    scopes = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert [r.summary["fetched_rows"] for r in scopes] == [2, 1, 2]
    assert [r.summary["submitted_rows"] for r in scopes] == [2, 1, 0]
    assert [r.summary["time_basis"] for r in scopes] == ["civil", "utc", "civil"]
    received = [
        r.summary
        for r in caplog.records
        if getattr(r, "event", None) == "fitbit_notification_received"
    ]
    published = [
        r.summary
        for r in caplog.records
        if getattr(r, "event", None) == "fitbit_notification_publish"
    ]
    pushes = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_push"]
    assert len({entry["source_notification_id"] for entry in received}) == 3
    for received_entry, published_entry, scope, push in zip(received, published, scopes, pushes):
        assert received_entry["source_notification_id"] == published_entry["source_notification_id"]
        assert published_entry["notification_id"] == scope.summary["notification_id"]
        assert published_entry["pubsub_message_id"] == push.summary["pubsub_message_id"]
        assert scope.summary["attempt_id"] == push.summary["attempt_id"]
    with closing(factory()) as warehouse:
        assert warehouse.query_rows(
            "SELECT record_id,value,fetched_at FROM base.fitbit_activity_interval ORDER BY record_id"
        ) == [("a", 12.0, NOW), ("b", 20.0, NOW + timedelta(seconds=1))]
        assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    summaries = json.dumps(
        [r.summary for r in caplog.records if hasattr(r, "summary")], default=str
    )
    for private in ("health-owner", "Bearer secret", "synthetic-token"):
        assert private not in summaries


def test_same_http_steps_coalesce_once_and_preserve_corrections_deletions_and_lineage(
    tmp_path, caplog
):
    path = str(tmp_path / "bundled-steps.duckdb")
    opened = []

    def factory():
        warehouse = Warehouse(duckdb.connect(path))
        opened.append(warehouse)
        return warehouse

    a = {
        "dataPointName": "users/me/dataTypes/steps/dataPoints/a",
        **steps(start="2026-09-26T14:00:00Z", end="2026-09-26T14:01:00Z"),
    }
    b = {
        "dataPointName": "users/me/dataTypes/steps/dataPoints/b",
        **steps(start="2026-09-26T14:01:00Z", end="2026-09-26T14:02:00Z"),
    }
    changed = {
        "dataPointName": "users/me/dataTypes/steps/dataPoints/a",
        **steps("20", start="2026-09-26T14:00:00Z", end="2026-09-26T14:01:00Z"),
    }
    transport = FakeTransport(
        [
            {"dataPoints": [a, b]},
            *({"dataPoints": [changed]} for _ in range(4)),
        ]
    )
    times = iter(NOW + timedelta(seconds=index) for index in range(5))
    runner = AcquisitionRunner(
        client=client(transport, clock=lambda: next(times)),
        warehouse_factory=factory,
        subject_key="self",
        clock=lambda: NOW + timedelta(hours=1),
    )
    subscription = "projects/test/subscriptions/fitbit"
    worker = TestClient(create_worker_app(runner=runner, subscription=subscription))

    class Publisher:
        def __init__(self):
            self.payloads = []

        def publish(self, topic, data):
            self.payloads.append(data)
            return self

        def result(self, *, timeout):
            return "message-" + str(len(self.payloads))

    publisher = Publisher()
    receiver = TestClient(
        create_pubsub_app(
            authenticator=GoogleHealthAuthenticator(
                authorization="Bearer secret",
                health_user_id="health-owner",
                subject_key="self",
                signatures=Signature(),
            ),
            notifications=PubSubNotifications(
                topic="projects/test/topics/fitbit", publisher=publisher
            ),
        )
    )
    full = notification()
    full["data"]["intervals"] = [
        {
            "civilIso8601TimeInterval": {
                "startTime": "2026-09-26T00:00:00",
                "endTime": "2026-09-27T00:00:00",
            }
        }
    ]
    partials = []
    for lower, upper in ((0, 15), (1, 2), (6, 9), (15, 16)):
        notice = notification()
        notice["data"]["intervals"] = [
            {
                "physicalTimeInterval": {
                    "startTime": f"2026-09-26T14:{lower:02d}:00Z",
                    "endTime": f"2026-09-26T14:{upper:02d}:00Z",
                }
            }
        ]
        partials.append(notice)
    partials[1]["data"].update(operation="DELETE", recordId="b", unknown="private-extra")
    resting = notification("daily-resting-heart-rate")
    resting["data"]["intervals"] = deepcopy(full["data"]["intervals"])
    batch = [full, *partials, deepcopy(full), resting]
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    for index, value in enumerate((full, batch, batch)):
        before = len(publisher.payloads)
        response = receiver.post(
            "/webhooks/fitbit",
            content=json.dumps(value),
            headers={
                "authorization": "Bearer secret",
                "content-type": "application/json",
                "google-health-api-signature": base64.b64encode(b"signed").decode(),
            },
        )
        assert response.status_code == 204
        step_count = 1 if index == 0 else 2
        assert len(publisher.payloads) - before == (1 if index == 0 else 3)
        payloads = publisher.payloads[before : before + step_count]
        assert all(json.loads(payload)["schema_version"] == 2 for payload in payloads)
        if index:
            assert {json.loads(payload)["windows"][0]["time_basis"] for payload in payloads} == {
                "utc",
                "civil",
            }
            assert (
                json.loads(publisher.payloads[-1])["windows"][0]["data_type"]
                == "daily-resting-heart-rate"
            )
        for message_index, payload in enumerate(payloads, start=before + 1):
            assert (
                worker.post(
                    "/notifications/fitbit",
                    json={
                        "message": {
                            "data": base64.b64encode(payload).decode(),
                            "messageId": f"message-{message_index}",
                        },
                        "subscription": subscription,
                    },
                ).status_code
                == 204
            )
        assert len(opened) == 2 * index + 1
    civil = 'steps.interval.civil_start_time >= "2026-09-26" AND steps.interval.civil_start_time < "2026-09-27"'
    utc = 'steps.interval.start_time >= "2026-09-26T14:00:00Z" AND steps.interval.start_time < "2026-09-26T14:16:00Z"'
    assert [parse_qs(urlsplit(call[1]).query)["filter"][0] for call in transport.calls] == [
        civil,
        civil,
        utc,
        civil,
        utc,
    ]
    scopes = [r for r in caplog.records if getattr(r, "event", None) == "fitbit_acquisition"]
    assert [r.summary["submitted_rows"] for r in scopes] == [2, 1, 0, 0, 0]
    with closing(factory()) as warehouse:
        assert warehouse.query_rows(
            "SELECT record_id,value FROM base.fitbit_activity_interval"
        ) == [("a", 20.0)]
        assert warehouse.query_rows("SELECT record_id FROM ops.fitbit_deleted_record") == [("b",)]
        assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
    received = [
        r.summary
        for r in caplog.records
        if getattr(r, "event", None) == "fitbit_notification_received"
    ]
    published = [
        r.summary
        for r in caplog.records
        if getattr(r, "event", None) == "fitbit_notification_publish"
    ]
    pushes = [r.summary for r in caplog.records if getattr(r, "event", None) == "fitbit_push"]
    assert len(received) == 15
    assert received[3]["operation"] == "DELETE" and received[3]["record_id"] == "b"
    for offset, published_index in ((1, 1), (8, 4)):
        step_sources = {r["source_notification_id"] for r in received[offset : offset + 6]}
        entries = published[published_index : published_index + 2]
        assert {
            source for entry in entries for source in entry["source_notification_ids"]
        } == step_sources
        assert all(entry["source_notification_id"] is None for entry in entries)
        assert [len(entry["source_notification_ids"]) for entry in entries] == [2, 4]
        assert [entry["time_basis"] for entry in entries] == ["civil", "utc"]
    for entry, scope, push in zip((published[index] for index in (0, 1, 2, 4, 5)), scopes, pushes):
        assert (
            entry["notification_id"] == scope.summary["notification_id"] == push["notification_id"]
        )
        assert entry["pubsub_message_id"] == push["pubsub_message_id"]
        assert scope.summary["attempt_id"] == push["attempt_id"]
    assert published[1]["notification_id"] != published[4]["notification_id"]
    assert received[1]["notification_hash"] == received[8]["notification_hash"]
    summaries = json.dumps(
        [r.summary for r in caplog.records if hasattr(r, "summary")], default=str
    )
    for private in ("health-owner", "Bearer secret", "synthetic-token", "private-extra"):
        assert private not in summaries


def test_slow_coalesced_scopes_can_each_commit_without_retry_starvation(tmp_path):
    path = str(tmp_path / "slow-scopes.duckdb")
    elapsed = [0.0]

    class API:
        def __init__(self):
            self.calls = []

        def fetch_snapshot(self, window, *, subject_key):
            self.calls.append(window)
            elapsed[0] += 200
            return Snapshot(subject_key, window, NOW + timedelta(seconds=elapsed[0]), ())

    api = API()
    runner = AcquisitionRunner(
        client=api,
        warehouse_factory=lambda: Warehouse(duckdb.connect(path)),
        subject_key="self",
        clock=lambda: NOW + timedelta(hours=1),
        monotonic=lambda: elapsed[0],
    )

    class Publisher:
        def __init__(self):
            self.payloads = []

        def publish(self, topic, data):
            self.payloads.append(data)
            return self

        def result(self, *, timeout):
            return "message-" + str(len(self.payloads))

    publisher = Publisher()
    receiver = TestClient(
        create_pubsub_app(
            authenticator=GoogleHealthAuthenticator(
                authorization="Bearer secret",
                health_user_id="health-owner",
                subject_key="self",
                signatures=Signature(),
            ),
            notifications=PubSubNotifications(
                topic="projects/test/topics/fitbit", publisher=publisher
            ),
        )
    )
    notices = [notification(), notification()]
    for index, notice in enumerate(notices):
        notice["data"]["intervals"] = [
            {
                "physicalTimeInterval": {
                    "startTime": f"2026-09-26T14:0{2 * index}:00Z",
                    "endTime": f"2026-09-26T14:0{2 * index + 1}:00Z",
                }
            }
        ]
    assert (
        receiver.post(
            "/webhooks/fitbit",
            content=json.dumps(notices),
            headers={
                "authorization": "Bearer secret",
                "content-type": "application/json",
                "google-health-api-signature": base64.b64encode(b"signed").decode(),
            },
        ).status_code
        == 204
    )
    worker = TestClient(
        create_worker_app(runner=runner, subscription="projects/test/subscriptions/fitbit")
    )
    for index, payload in enumerate(publisher.payloads):
        assert (
            worker.post(
                "/notifications/fitbit",
                json={
                    "message": {
                        "data": base64.b64encode(payload).decode(),
                        "messageId": f"message-{index}",
                    },
                    "subscription": "projects/test/subscriptions/fitbit",
                },
            ).status_code
            == 204
        )
    assert len(api.calls) == 2
    with closing(Warehouse(duckdb.connect(path))) as warehouse:
        assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_coverage") == 2
        assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
