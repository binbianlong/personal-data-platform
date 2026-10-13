import json
from datetime import UTC, datetime, timedelta

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


def notification():
    at = datetime(2026, 10, 1, tzinfo=UTC)
    return Notification("notification", "self", (Window("steps", at, at + timedelta(days=1)),), at)


def transport(publisher=None):
    from personal_data_platform.sources.fitbit.notifications import PubSubNotifications

    return PubSubNotifications(
        topic="projects/test/topics/fitbit",
        publisher=publisher,
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
    assert "acquisition_mode" not in data


def test_unknown_publish_result_is_a_failure():
    publisher = Publisher()
    publisher.future = Future(TimeoutError("unknown"))
    with pytest.raises(TimeoutError):
        transport(publisher=publisher).publish(notification())


@pytest.mark.parametrize("wide", [False, True])
def test_consumer_rejects_more_than_one_day_or_one_window(wide):
    from personal_data_platform.sources.fitbit.notifications import decode_notification

    publisher = Publisher()
    transport(publisher=publisher).publish(notification())
    data = json.loads(publisher.published[0][1])
    if wide:
        data["windows"][0]["end"] = "2026-10-03T00:00:00+00:00"
    else:
        data["windows"].append({**data["windows"][0], "data_type": "heart-rate"})
    with pytest.raises(ValueError):
        decode_notification(json.dumps(data).encode())


def test_bounded_heart_rate_round_trips_in_schema_one():
    from personal_data_platform.sources.fitbit.notifications import decode_notification

    at = datetime(2026, 10, 1, tzinfo=UTC)
    notice = Notification(
        "bounded",
        "self",
        (Window("heart-rate", at, at + timedelta(minutes=2)),),
        at + timedelta(minutes=3),
        acquisition_mode="bounded_minutes",
    )
    publisher = Publisher()
    transport(publisher).publish(notice)
    payload = publisher.published[0][1]
    assert json.loads(payload)["schema_version"] == 1
    assert json.loads(payload)["acquisition_mode"] == "bounded_minutes"
    assert decode_notification(payload) == notice
    legacy = json.loads(payload)
    del legacy["acquisition_mode"]
    assert decode_notification(json.dumps(legacy).encode()).acquisition_mode == "legacy"


@pytest.mark.parametrize(
    "mode,kind,start",
    [
        ("unknown", "heart-rate", "2026-10-01T00:00:00Z"),
        ("bounded_minutes", "steps", "2026-10-01T00:00:00Z"),
        ("bounded_minutes", "heart-rate", "2026-10-01T00:00:01Z"),
    ],
)
def test_decoder_rejects_invalid_acquisition_mode(mode, kind, start):
    from personal_data_platform.sources.fitbit.notifications import decode_notification

    payload = {
        "schema_version": 1,
        "notification_id": "bad",
        "subject_key": "self",
        "received_at": "2026-10-01T00:03:00Z",
        "acquisition_mode": mode,
        "windows": [{"data_type": kind, "start": start, "end": "2026-10-01T00:02:00Z"}],
    }
    with pytest.raises(ValueError):
        decode_notification(json.dumps(payload).encode())
