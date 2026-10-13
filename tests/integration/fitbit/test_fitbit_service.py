from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from personal_data_platform.sources.fitbit.models import Window

NOW = datetime(2026, 9, 27, tzinfo=UTC)
WINDOW = Window("steps", NOW, NOW.replace(hour=1))


def test_receiver_requires_all_publishes_before_204():
    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import Verification, VerifiedNotification

    class Auth:
        def authenticate(self, **kwargs):
            if kwargs["body"] == b'{"type":"verification"}':
                return Verification()
            return VerifiedNotification("self", (WINDOW,), groups=((WINDOW,), (WINDOW,)))

    class Publisher:
        def __init__(self):
            self.calls = 0
            self.fail = True

        def publish(self, notification):
            self.calls += 1
            if self.fail and self.calls == 2:
                raise TimeoutError("unknown publish result")
            return "message"

    publisher = Publisher()
    client = TestClient(create_pubsub_app(authenticator=Auth(), notifications=publisher))
    assert client.post("/webhooks/fitbit", content="{}").status_code == 503
    assert publisher.calls == 2
    publisher.fail = False
    assert client.post("/webhooks/fitbit", content="{}").status_code == 204
    assert client.post("/webhooks/fitbit", content='{"type":"verification"}').status_code == 201
    assert publisher.calls == 4
    assert client.post("/internal/tasks/fitbit", content="{}").status_code == 404


@pytest.mark.parametrize("groups", [1, 2])
def test_receiver_rejects_expansion_before_publishing(groups):
    from datetime import timedelta

    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import VerifiedNotification

    start = datetime(2020, 1, 1, tzinfo=UTC)
    days = 1001 if groups == 1 else 600
    window = Window("daily-resting-heart-rate", start, start + timedelta(days=days))

    class Auth:
        def authenticate(self, **kwargs):
            return VerifiedNotification("self", (window,), groups=((window,),) * groups)

    class Publisher:
        def __init__(self):
            self.published = []

        def publish(self, notification):
            self.published.append(notification)
            return "message"

    publisher = Publisher()
    response = TestClient(create_pubsub_app(authenticator=Auth(), notifications=publisher)).post(
        "/webhooks/fitbit", content="{}"
    )
    assert response.status_code == 400
    assert publisher.published == []


def test_partial_publish_returns_503_and_retry_preserves_units(caplog):
    from datetime import timedelta

    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import VerifiedNotification

    start = datetime(2026, 10, 1, 15, tzinfo=UTC)
    window = Window("steps", start, start + timedelta(days=3))

    class Auth:
        def authenticate(self, **kwargs):
            return VerifiedNotification("self", (window,))

    class Publisher:
        def __init__(self):
            self.published = []

        def publish(self, notification):
            self.published.append(notification)
            if len(self.published) == 2:
                raise TimeoutError("unknown publish result")
            return "message"

    publisher = Publisher()
    client = TestClient(create_pubsub_app(authenticator=Auth(), notifications=publisher))
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    assert client.post("/webhooks/fitbit", content="{}").status_code == 503
    assert client.post("/webhooks/fitbit", content="{}").status_code == 204
    assert len(publisher.published) == 5
    assert [n.windows for n in publisher.published[:2]] == [
        n.windows for n in publisher.published[2:4]
    ]
    assert [n.windows[0] for n in publisher.published[2:]] == [
        Window("steps", start, start + timedelta(days=1)),
        Window("steps", start + timedelta(days=1), start + timedelta(days=2)),
        Window("steps", start + timedelta(days=2), start + timedelta(days=3)),
    ]
    entries = [
        r for r in caplog.records if getattr(r, "event", None) == "fitbit_notification_publish"
    ]
    assert len(entries) == 5
    assert [r.status for r in entries] == [
        "succeeded",
        "unknown",
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert entries[1].summary["notification_id"] == publisher.published[1].notification_id
    assert entries[1].summary["pubsub_message_id"] is None
    assert entries[1].summary["error_type"] == "TimeoutError"
    assert (
        entries[0].summary["source_notification_id"] == entries[1].summary["source_notification_id"]
    )
    assert (
        entries[0].summary["source_notification_id"] != entries[2].summary["source_notification_id"]
    )


def test_receiver_logs_source_intervals_and_correlates_repeated_deliveries(caplog):
    import base64
    import json

    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import GoogleHealthAuthenticator
    from tests.unit.fitbit.test_fitbit_webhook import Signature, notification

    value = notification()
    value["data"]["unknown"] = "private-extra"
    value["data"]["intervals"].append(
        {
            "physicalTimeInterval": {
                "startTime": "2026-09-27T00:00:00Z",
                "endTime": "2026-09-27T00:05:00Z",
            }
        }
    )
    auth = GoogleHealthAuthenticator(
        authorization="Bearer secret",
        health_user_id="health-owner",
        subject_key="self",
        signatures=Signature(),
    )

    class Publisher:
        def publish(self, notification):
            return "message-" + notification.notification_id

    client = TestClient(create_pubsub_app(authenticator=auth, notifications=Publisher()))
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    for _ in range(2):
        response = client.post(
            "/webhooks/fitbit",
            content=json.dumps(value),
            headers={
                "authorization": "Bearer secret",
                "content-type": "application/json",
                "google-health-api-signature": base64.b64encode(b"signed").decode(),
            },
        )
        assert response.status_code == 204
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
    assert len(received) == 4
    assert received[0]["physical_start"] == "2026-09-26T14:00:00+00:00"
    assert received[1]["physical_start"] == "2026-09-27T00:00:00+00:00"
    assert received[0]["civil_start"] == "2026-09-26T23:00:00"
    assert received[1]["civil_start"] is None
    assert received[0]["interval_index"] == 0
    assert received[1]["interval_index"] == 1
    assert received[0]["interval_count"] == 2
    assert received[0]["notification_hash"] == received[2]["notification_hash"]
    assert received[0]["delivery_id"] != received[2]["delivery_id"]
    assert received[0]["source_notification_id"] != received[2]["source_notification_id"]
    assert published[0]["source_notification_id"] == received[0]["source_notification_id"]
    assert published[0]["pubsub_message_id"] == "message-" + published[0]["notification_id"]
    serialized = json.dumps(received + published)
    for private in ("health-owner", "Bearer secret", "private-extra"):
        assert private not in serialized
