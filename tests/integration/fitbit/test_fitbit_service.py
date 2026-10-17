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
@pytest.mark.parametrize("kind", ["steps", "daily-resting-heart-rate"])
def test_receiver_rejects_expansion_before_coalescing_or_publishing(groups, kind):
    from datetime import timedelta

    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import (
        NotificationObservation,
        VerifiedNotification,
    )

    start = datetime(2020, 1, 1, tzinfo=UTC)
    days = 1001 if groups == 1 else 600
    window = Window(kind, start, start + timedelta(days=days))

    class Auth:
        def authenticate(self, **kwargs):
            return VerifiedNotification(
                "self",
                (window,),
                groups=((window,),) * groups,
                observations=(NotificationObservation(kind, "UPSERT", None, ()),) * groups,
                group_modes=("bounded_steps" if kind == "steps" else "legacy",) * groups,
            )

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


@pytest.mark.parametrize("future", [False, True])
@pytest.mark.parametrize("kind", ["heart-rate", "steps"])
def test_receiver_bounds_notifications_and_only_coalesces_steps(kind, future, monkeypatch, caplog):
    import base64
    import json

    from personal_data_platform.sources.fitbit import service
    from personal_data_platform.sources.fitbit.webhook import GoogleHealthAuthenticator
    from tests.unit.fitbit.test_fitbit_webhook import Signature, notification

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(second=10)

    monkeypatch.setattr(service, "datetime", Clock)
    values = [notification(kind), notification(kind)]
    for index, value in enumerate(values):
        value["data"]["intervals"][0]["physicalTimeInterval"] = {
            "startTime": "2026-09-27T00:00:20Z" if future else f"2026-09-26T14:00:{20 + index}Z",
            "endTime": "2026-09-27T00:00:30Z" if future else "2026-09-26T14:01:00.000000001Z",
        }
    auth = GoogleHealthAuthenticator(
        authorization="Bearer secret",
        health_user_id="health-owner",
        subject_key="self",
        signatures=Signature(),
    )

    class Publisher:
        def __init__(self):
            self.published = []

        def publish(self, notification):
            self.published.append(notification)
            return "message-" + notification.notification_id

    publisher = Publisher()
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    response = TestClient(
        service.create_pubsub_app(authenticator=auth, notifications=publisher)
    ).post(
        "/webhooks/fitbit",
        content=json.dumps(values),
        headers={
            "authorization": "Bearer secret",
            "content-type": "application/json",
            "google-health-api-signature": base64.b64encode(b"signed").decode(),
        },
    )
    if future:
        assert response.status_code == 400
        assert publisher.published == []
        assert not any(
            getattr(r, "event", None) == "fitbit_notification_received" for r in caplog.records
        )
    else:
        assert response.status_code == 204
        count = 2 if kind == "heart-rate" else 1
        assert len(publisher.published) == count
        assert len({n.notification_id for n in publisher.published}) == count
        if kind == "heart-rate":
            expected = Window(
                kind, NOW.replace(day=26, hour=14), NOW.replace(day=26, hour=14, minute=2)
            )
            assert all(n.windows == (expected,) for n in publisher.published)
        else:
            assert [n.windows[0] for n in publisher.published] == [
                Window(
                    kind,
                    NOW.replace(day=26, hour=14, second=20 + index),
                    NOW.replace(day=26, hour=14, minute=1, microsecond=1),
                )
                for index in range(1)
            ]
        mode = "bounded_minutes" if kind == "heart-rate" else "bounded_steps"
        assert all(n.acquisition_mode == mode for n in publisher.published)
        received = [
            r.summary
            for r in caplog.records
            if getattr(r, "event", None) == "fitbit_notification_received"
        ]
        assert received[0]["physical_start"] == "2026-09-26T14:00:20+00:00"
        assert received[0]["physical_end"] == "2026-09-26T14:01:00.000000001+00:00"
        assert len({s["source_notification_id"] for s in received}) == 2


def test_receiver_keeps_large_coalesced_steps_delivery_independently_retryable(caplog):
    import base64
    import json
    from datetime import timedelta

    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import GoogleHealthAuthenticator
    from tests.unit.fitbit.test_fitbit_webhook import Signature, notification

    values = [notification(), notification()]
    start = NOW.replace(day=26, hour=0)
    intervals = [
        {
            "physicalTimeInterval": {
                "startTime": (start + timedelta(minutes=2 * index)).isoformat(),
                "endTime": (start + timedelta(minutes=2 * index + 1)).isoformat(),
            }
        }
        for index in range(501)
    ]
    values[0]["data"]["intervals"] = intervals[:250]
    values[1]["data"]["intervals"] = intervals[250:]

    class Publisher:
        def __init__(self):
            self.published = []

        def publish(self, notice):
            self.published.append(notice)
            return "message-" + str(len(self.published))

    publisher = Publisher()
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    response = TestClient(
        create_pubsub_app(
            authenticator=GoogleHealthAuthenticator(
                authorization="Bearer secret",
                health_user_id="health-owner",
                subject_key="self",
                signatures=Signature(),
            ),
            notifications=publisher,
        )
    ).post(
        "/webhooks/fitbit",
        content=json.dumps(values),
        headers={
            "authorization": "Bearer secret",
            "content-type": "application/json",
            "google-health-api-signature": base64.b64encode(b"signed").decode(),
        },
    )
    assert response.status_code == 204
    assert [len(notice.windows) for notice in publisher.published] == [1] * 501
    scopes = [window for notice in publisher.published for window in notice.windows]
    assert len(set(scopes)) == 501
    assert scopes[0] == Window("steps", start, start + timedelta(minutes=1))
    assert scopes[-1] == Window(
        "steps", start + timedelta(minutes=1000), start + timedelta(minutes=1001)
    )
    published = [
        r.summary
        for r in caplog.records
        if getattr(r, "event", None) == "fitbit_notification_publish"
    ]
    assert len(published) == 501
    assert all(entry["source_notification_ids"] for entry in published)


def test_coalesced_steps_partial_publish_preserves_day_boundaries_and_all_source_ids(caplog):
    import base64
    import json

    from personal_data_platform.sources.fitbit.service import create_pubsub_app
    from personal_data_platform.sources.fitbit.webhook import GoogleHealthAuthenticator
    from tests.unit.fitbit.test_fitbit_webhook import Signature, notification

    class Publisher:
        def __init__(self):
            self.published = []
            self.fail = True

        def publish(self, notice):
            self.published.append(notice)
            if self.fail and len(self.published) == 2:
                raise TimeoutError("unknown publish result")
            return "confirmed-message"

    publisher = Publisher()
    receiver = TestClient(
        create_pubsub_app(
            authenticator=GoogleHealthAuthenticator(
                authorization="Bearer secret",
                health_user_id="health-owner",
                subject_key="self",
                signatures=Signature(),
            ),
            notifications=publisher,
        )
    )
    caplog.set_level("INFO", logger="personal_data_platform.sources.fitbit")
    for expected in (503, 204):
        response = receiver.post(
            "/webhooks/fitbit",
            content=json.dumps([notification(), notification()]),
            headers={
                "authorization": "Bearer secret",
                "content-type": "application/json",
                "google-health-api-signature": base64.b64encode(b"signed").decode(),
            },
        )
        assert response.status_code == expected
        publisher.fail = False
    assert len(publisher.published) == 4
    expected_windows = (
        Window("steps", NOW.replace(day=26, hour=14), NOW.replace(day=26, hour=15)),
        Window("steps", NOW.replace(day=26, hour=15), NOW.replace(day=26, hour=22)),
    )
    assert [notice.windows[0] for notice in publisher.published] == list(expected_windows) * 2
    entries = [
        r for r in caplog.records if getattr(r, "event", None) == "fitbit_notification_publish"
    ]
    received = [
        r.summary
        for r in caplog.records
        if getattr(r, "event", None) == "fitbit_notification_received"
    ]
    assert [r.status for r in entries] == ["succeeded", "unknown", "succeeded", "succeeded"]
    assert entries[1].summary["pubsub_message_id"] is None
    assert entries[1].summary["error_type"] == "TimeoutError"
    assert entries[2].summary["pubsub_message_id"] == "confirmed-message"
    for index, entry in enumerate(entries):
        offset = 0 if index < 2 else 2
        source_ids = {r["source_notification_id"] for r in received[offset : offset + 2]}
        assert set(entry.summary["source_notification_ids"]) == source_ids
    assert entries[0].summary["delivery_id"] != entries[2].summary["delivery_id"]
    assert entries[0].summary["notification_id"] != entries[2].summary["notification_id"]
