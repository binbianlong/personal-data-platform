import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_data_platform.sources.fitbit.models import Notification, Window


class Future:
    def __init__(self, error=None):
        self.error = error
        self.waited = False

    def result(self, *, timeout):
        assert 0 < timeout <= 30
        self.waited = True
        if self.error:
            raise self.error
        return "message"


class Publisher:
    def __init__(self):
        self.future = Future()
        self.published = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))
        return self.future


class Subscriber:
    def __init__(self):
        self.response = SimpleNamespace(received_messages=[])
        self.extended = []
        self.acked = []

    def pull(self, *, request, timeout, retry):
        self.request = request
        assert retry is None
        return self.response

    def modify_ack_deadline(self, *, request, timeout, retry):
        self.extended.append(request)

    def acknowledge(self, *, request, timeout, retry):
        self.acked.append(request)


def notification():
    at = datetime(2026, 10, 1, tzinfo=UTC)
    return Notification("notification", "self", (Window("steps", at, at + timedelta(days=1)),), at)


def transport(publisher=None, subscriber=None):
    from personal_data_platform.sources.fitbit.notifications import PubSubNotifications

    return PubSubNotifications(
        topic="projects/test/topics/fitbit",
        subscription="projects/test/subscriptions/fitbit",
        publisher=publisher,
        subscriber=subscriber,
    )


def test_publish_waits_for_confirmed_result_and_preserves_identity():
    publisher = Publisher()
    value = transport(publisher=publisher)
    assert value.publish(notification()) == "message"
    assert publisher.future.waited
    data = json.loads(publisher.published[0][1])
    assert data["notification_id"] == "notification"
    assert data["schema_version"] == 1
    assert data["windows"][0]["data_type"] == "steps"


def test_unknown_publish_result_is_a_failure():
    publisher = Publisher()
    publisher.future = Future(TimeoutError("unknown"))
    with pytest.raises(TimeoutError):
        transport(publisher=publisher).publish(notification())


def test_pull_round_trip_and_ack_deadline_limits():
    publisher, subscriber = Publisher(), Subscriber()
    value = transport(publisher, subscriber)
    value.publish(notification())
    subscriber.response.received_messages = [
        SimpleNamespace(
            ack_id="ack",
            message=SimpleNamespace(message_id="message", data=publisher.published[0][1]),
        )
    ]
    deliveries = value.pull(limit=5, timeout_seconds=1)
    assert deliveries[0].notification == notification()
    value.extend(("ack",), seconds=600)
    assert subscriber.extended[-1]["ack_deadline_seconds"] == 600
    with pytest.raises(ValueError):
        value.extend(("ack",), seconds=601)
    value.ack(("ack",))
    assert subscriber.acked[-1]["ack_ids"] == ["ack"]


def test_malformed_delivery_is_released_without_blocking_valid_delivery():
    publisher, subscriber = Publisher(), Subscriber()
    value = transport(publisher, subscriber)
    value.publish(notification())
    subscriber.response.received_messages = [
        SimpleNamespace(ack_id="bad", message=SimpleNamespace(message_id="bad", data=b"{}")),
        SimpleNamespace(
            ack_id="good",
            message=SimpleNamespace(message_id="good", data=publisher.published[0][1]),
        ),
    ]
    deliveries = value.pull(limit=2, timeout_seconds=1)
    assert [d.ack_id for d in deliveries] == ["good"]
    assert subscriber.extended == [
        {
            "subscription": "projects/test/subscriptions/fitbit",
            "ack_ids": ["bad"],
            "ack_deadline_seconds": 0,
        }
    ]
    assert value.invalid_count == 1
