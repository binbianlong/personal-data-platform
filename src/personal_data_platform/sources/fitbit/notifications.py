"""Regional Pub/Sub delivery boundary for validated acquisition notifications."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import timedelta
from typing import Protocol, cast

from google.cloud import pubsub_v1

from .models import Notification, Window, object_dict, parse_time, string

ENDPOINT = "pubsub.us-west1.rep.googleapis.com"


class PublishResult(Protocol):
    def result(self, *, timeout: float) -> str: ...


class Publisher(Protocol):
    def publish(self, topic: str, data: bytes) -> PublishResult: ...


def decode_notification(payload: bytes) -> Notification:
    if len(payload) > 1024 * 1024:
        raise ValueError("notification exceeds size limit")
    data = object_dict(json.loads(payload))
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported notification schema")
    windows = data["windows"]
    if not isinstance(windows, list) or len(windows) != 1:
        raise ValueError("invalid notification windows")
    notification = Notification(
        string(data["notification_id"]),
        string(data["subject_key"]),
        tuple(
            Window(
                string(row["data_type"]),
                parse_time(string(row["start"])),
                parse_time(string(row["end"])),
            )
            for row in (object_dict(value) for value in windows)
        ),
        parse_time(string(data["received_at"])),
    )
    if notification.windows[0].end - notification.windows[0].start > timedelta(days=1):
        raise ValueError("notification exceeds one day")
    return notification


class PubSubNotifications:
    def __init__(
        self,
        *,
        topic: str = "",
        endpoint: str = ENDPOINT,
        publisher: Publisher | None = None,
    ) -> None:
        if endpoint != ENDPOINT:
            raise ValueError("Fitbit Pub/Sub endpoint must be the us-west1 regional endpoint")
        self.topic, self.endpoint = topic, endpoint
        self._publisher = publisher

    @classmethod
    def from_env(cls) -> PubSubNotifications:
        return cls(
            topic=os.environ.get("PDP_FITBIT_PUBSUB_TOPIC", ""),
            endpoint=os.environ.get("PDP_FITBIT_PUBSUB_ENDPOINT", ENDPOINT),
        )

    def _publishing_client(self) -> Publisher:
        if not self.topic.startswith("projects/") or "/topics/" not in self.topic:
            raise ValueError("PDP_FITBIT_PUBSUB_TOPIC must be a full resource name")
        if self._publisher is None:
            self._publisher = cast(
                Publisher, pubsub_v1.PublisherClient(client_options={"api_endpoint": self.endpoint})
            )
        return self._publisher

    def publish(self, notification: Notification) -> str:
        data = json.dumps(
            {"schema_version": 1, **asdict(notification)},
            default=lambda value: value.isoformat(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(data) > 1024 * 1024:
            raise ValueError("notification exceeds size limit")
        return self._publishing_client().publish(self.topic, data).result(timeout=30)
