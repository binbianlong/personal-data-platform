"""Regional Pub/Sub delivery boundary for validated acquisition notifications."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Protocol, cast

import google.cloud.pubsub_v1 as pubsub_v1
from google.api_core.exceptions import DeadlineExceeded

from .models import Notification, Window, object_dict, parse_time, string

LOGGER = logging.getLogger(__name__)
ENDPOINT = "pubsub.us-west1.rep.googleapis.com"


@dataclass(frozen=True, slots=True)
class Delivery:
    notification: Notification
    ack_id: str
    message_id: str


class PublishResult(Protocol):
    def result(self, *, timeout: float) -> str: ...


class Publisher(Protocol):
    def publish(self, topic: str, data: bytes) -> PublishResult: ...


class Message(Protocol):
    @property
    def message_id(self) -> str: ...
    @property
    def data(self) -> bytes: ...


class ReceivedMessage(Protocol):
    @property
    def ack_id(self) -> str: ...
    @property
    def message(self) -> Message: ...


class PullResult(Protocol):
    @property
    def received_messages(self) -> list[ReceivedMessage]: ...


class Subscriber(Protocol):
    def pull(self, *, request: dict[str, object], timeout: float, retry: None) -> PullResult: ...
    def modify_ack_deadline(
        self, *, request: dict[str, object], timeout: float, retry: None
    ) -> None: ...
    def acknowledge(self, *, request: dict[str, object], timeout: float, retry: None) -> None: ...


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
        subscription: str = "",
        endpoint: str = ENDPOINT,
        publisher: Publisher | None = None,
        subscriber: Subscriber | None = None,
    ) -> None:
        if endpoint != ENDPOINT:
            raise ValueError("Fitbit Pub/Sub endpoint must be the us-west1 regional endpoint")
        self.topic, self.subscription, self.endpoint = topic, subscription, endpoint
        self._publisher, self._subscriber = publisher, subscriber
        self.invalid_count = 0

    @classmethod
    def from_env(cls) -> PubSubNotifications:
        return cls(
            topic=os.environ.get("PDP_FITBIT_PUBSUB_TOPIC", ""),
            subscription=os.environ.get("PDP_FITBIT_PUBSUB_SUBSCRIPTION", ""),
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

    def _subscription_client(self) -> Subscriber:
        if (
            not self.subscription.startswith("projects/")
            or "/subscriptions/" not in self.subscription
        ):
            raise ValueError("PDP_FITBIT_PUBSUB_SUBSCRIPTION must be a full resource name")
        if self._subscriber is None:
            self._subscriber = cast(
                Subscriber,
                pubsub_v1.SubscriberClient(client_options={"api_endpoint": self.endpoint}),
            )
        return self._subscriber

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

    def pull(self, *, limit: int, timeout_seconds: float) -> tuple[Delivery, ...]:
        if not 1 <= limit <= 500 or not 0 < timeout_seconds <= 30:
            raise ValueError("invalid notification pull bounds")
        try:
            response = self._subscription_client().pull(
                request={"subscription": self.subscription, "max_messages": limit},
                timeout=timeout_seconds,
                retry=None,
            )
        except DeadlineExceeded:
            return ()
        deliveries = []
        invalid = []
        for message in response.received_messages:
            try:
                notification = decode_notification(message.message.data)
                deliveries.append(
                    Delivery(notification, message.ack_id, message.message.message_id)
                )
            except (ValueError, KeyError, TypeError):
                invalid.append(message.ack_id)
                self.invalid_count += 1
                LOGGER.error(
                    "fitbit invalid notification message_id=%s", message.message.message_id
                )
        if invalid:
            self.extend(tuple(invalid), seconds=0)
        return tuple(deliveries)

    def extend(self, ack_ids: tuple[str, ...], *, seconds: int) -> None:
        if not 0 <= seconds <= 600:
            raise ValueError("ack extension must be between 0 and 600 seconds")
        if ack_ids:
            self._subscription_client().modify_ack_deadline(
                request={
                    "subscription": self.subscription,
                    "ack_ids": list(ack_ids),
                    "ack_deadline_seconds": seconds,
                },
                timeout=20,
                retry=None,
            )

    def ack(self, ack_ids: tuple[str, ...]) -> None:
        if ack_ids:
            self._subscription_client().acknowledge(
                request={
                    "subscription": self.subscription,
                    "ack_ids": list(ack_ids),
                },
                timeout=20,
                retry=None,
            )
