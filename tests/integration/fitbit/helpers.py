"""Synthetic Pub/Sub deliveries through the real Fitbit pull worker."""

import json
from dataclasses import asdict

from personal_data_platform.sources.fitbit.batch import NotificationDrainer
from tests.unit.fitbit.test_fitbit_batch import Subscriber, raw_message

SUBSCRIPTION = "projects/test/subscriptions/fitbit"


def drainer(runner, subscriber):
    return NotificationDrainer(runner=runner, subscriber=subscriber, subscription=SUBSCRIPTION)


def drain_notification(runner, notice, *, subscriber=None):
    if subscriber is None:
        subscriber = Subscriber()
    version = 2 if notice.acquisition_mode == "bounded_steps" else 1
    payload = json.dumps({"schema_version": version, **asdict(notice)}, default=str).encode()
    message = raw_message("notice", payload=payload)
    message.message.message_id = "message"
    subscriber.responses.append([message])
    return drainer(runner, subscriber).run()
