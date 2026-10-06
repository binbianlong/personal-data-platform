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


def test_partial_publish_returns_503_and_retry_preserves_units():
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
