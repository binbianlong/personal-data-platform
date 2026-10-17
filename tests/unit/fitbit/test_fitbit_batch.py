from datetime import UTC, datetime, timedelta

from personal_data_platform.sources.fitbit.models import Notification, Window

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def queued(identity, start, end, *, kind="steps", mode="bounded_steps", basis="utc"):
    from personal_data_platform.sources.fitbit.batch import QueuedNotification

    notice = Notification(identity, "self", (Window(kind, start, end, basis),), NOW, mode)
    return QueuedNotification(notice, "secret-ack-" + identity, "message-" + identity)


def test_group_overlaps_containment_and_adjacency_but_preserves_day_and_mode():
    from personal_data_platform.sources.fitbit.batch import group_notifications

    start = NOW - timedelta(hours=1)
    messages = [
        queued("a", start, start + timedelta(minutes=2)),
        queued("b", start + timedelta(minutes=1), start + timedelta(minutes=3)),
        queued("c", start + timedelta(minutes=3), start + timedelta(minutes=4)),
        queued("d", start, start + timedelta(minutes=1)),
        queued("legacy", start, start + timedelta(minutes=1), mode="legacy"),
        queued("next", NOW + timedelta(hours=3), NOW + timedelta(hours=3, minutes=1)),
    ]
    units, deferred = group_notifications(messages, now=NOW)
    assert not deferred and len(units) == 3
    merged = units[0]
    assert merged.notification.windows == (Window("steps", start, start + timedelta(minutes=4)),)
    assert {m.notification.notification_id for m in merged.originals} == {"a", "b", "c", "d"}
    assert len(merged.notification.windows) == 1


def test_daily_and_legacy_only_exact_original_ranges_deduplicate():
    from personal_data_platform.sources.fitbit.batch import group_notifications

    start = NOW.replace(hour=0)
    messages = [
        queued("a", start, start + timedelta(days=1), kind="sleep", mode="legacy"),
        queued("b", start, start + timedelta(days=1), kind="sleep", mode="legacy"),
        queued("c", start, start + timedelta(minutes=1), mode="legacy"),
        queued("d", start + timedelta(minutes=1), start + timedelta(minutes=2), mode="legacy"),
    ]
    units, _ = group_notifications(messages, now=NOW)
    assert len(units) == 3
    assert len(units[0].originals) == 2
    assert units[1].notification.windows == units[2].notification.windows


def test_incomplete_bounded_minute_is_excluded_before_merge_but_legacy_is_ready():
    from personal_data_platform.sources.fitbit.batch import group_notifications

    messages = [
        queued("ready", NOW - timedelta(minutes=2), NOW, kind="heart-rate", mode="bounded_minutes"),
        queued(
            "future",
            NOW - timedelta(minutes=1),
            NOW + timedelta(minutes=1),
            kind="heart-rate",
            mode="bounded_minutes",
        ),
        queued(
            "legacy",
            NOW - timedelta(minutes=1),
            NOW + timedelta(minutes=1),
            kind="heart-rate",
            mode="legacy",
        ),
    ]
    units, deferred = group_notifications(messages, now=NOW + timedelta(seconds=30))
    assert len(units) == 2
    assert [m.notification.notification_id for m in deferred] == ["future"]
    assert units[0].notification.windows == messages[0].notification.windows


def test_civil_days_and_noncanonical_cross_day_windows_stay_separate():
    from personal_data_platform.sources.fitbit.batch import group_notifications

    day = NOW.replace(hour=0)
    midnight = NOW.replace(hour=15)
    messages = [
        queued("civil1", day, day + timedelta(days=1), basis="civil"),
        queued("civil2", day + timedelta(days=1), day + timedelta(days=2), basis="civil"),
        queued("cross", midnight - timedelta(minutes=1), midnight + timedelta(minutes=1)),
        queued("same", midnight - timedelta(minutes=1), midnight),
    ]
    units, deferred = group_notifications(messages, now=NOW)
    assert len(units) == 4 and not deferred
    assert all(len(unit.originals) == 1 for unit in units)


def raw_message(identity, *, payload=None):
    import json
    from dataclasses import asdict
    from types import SimpleNamespace

    message = queued(identity, NOW - timedelta(minutes=2), NOW - timedelta(minutes=1))
    data = (
        payload
        if payload is not None
        else json.dumps({"schema_version": 2, **asdict(message.notification)}, default=str).encode()
    )
    return SimpleNamespace(
        ack_id=message.ack_id, message=SimpleNamespace(data=data, message_id=message.message_id)
    )


class Subscriber:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.pulls = []
        self.acks = []

    def pull(self, *, request, retry, timeout):
        from types import SimpleNamespace

        self.pulls.append((request, retry, timeout))
        return SimpleNamespace(received_messages=self.responses.pop(0) if self.responses else [])

    def acknowledge(self, *, request, retry, timeout):
        self.acks.extend(request["ack_ids"])


def test_collection_stops_at_message_limit_without_retry_or_ack():
    from personal_data_platform.sources.fitbit.batch import collect_notifications

    subscriber = Subscriber([[raw_message(str(i)) for i in range(100)] for _ in range(11)])
    collected = collect_notifications(
        subscriber, "subscription", subject_key="self", deadline=10, monotonic=lambda: 0
    )
    assert len(collected.messages) == collected.received == 1000
    assert len(subscriber.pulls) == 10 and not subscriber.acks
    assert all(r[0]["max_messages"] == 100 and r[1] is None and r[2] <= 10 for r in subscriber.pulls)


def test_collection_receives_backlog_that_takes_more_than_two_seconds_to_arrive():
    from google.api_core.exceptions import DeadlineExceeded

    from personal_data_platform.sources.fitbit.batch import collect_notifications

    elapsed = [0.0]

    class DelayedSubscriber(Subscriber):
        def pull(self, *, request, retry, timeout):
            from types import SimpleNamespace

            self.pulls.append((request, retry, timeout))
            wait = 3.0 if len(self.pulls) == 1 else 20.0
            elapsed[0] += min(wait, timeout)
            if timeout < wait:
                raise DeadlineExceeded("secret transport text")
            return SimpleNamespace(received_messages=[raw_message("delayed")])

    subscriber = DelayedSubscriber()
    result = collect_notifications(
        subscriber, "subscription", subject_key="self", deadline=10, monotonic=lambda: elapsed[0]
    )
    assert result.received == len(result.messages) == 1
    assert result.messages[0].message_id == "message-delayed"
    assert elapsed[0] == 10 and not subscriber.acks
    assert [call[2] for call in subscriber.pulls] == [10, 7]
    assert all(call[1] is None for call in subscriber.pulls)


def test_collection_rejects_invalid_subject_and_byte_overflow_without_ack():
    from personal_data_platform.sources.fitbit.batch import collect_notifications

    subscriber = Subscriber(
        [
            [
                raw_message("bad", payload=b"not-json"),
                *[raw_message(str(i), payload=b"x" * 1024 * 1024) for i in range(9)],
            ]
        ]
    )
    result = collect_notifications(
        subscriber, "subscription", subject_key="self", deadline=10, monotonic=lambda: 0
    )
    assert not result.messages and result.received == 10 and result.rejected == 10
    assert len(subscriber.pulls) == 1 and not subscriber.acks


def test_collection_deadline_exceeded_is_empty_and_time_budget_limits_rpc():
    from google.api_core.exceptions import DeadlineExceeded

    from personal_data_platform.sources.fitbit.batch import collect_notifications

    class TimeoutSubscriber(Subscriber):
        def pull(self, **kwargs):
            self.pulls.append(kwargs)
            raise DeadlineExceeded("secret transport text")

    subscriber = TimeoutSubscriber()
    result = collect_notifications(
        subscriber, "subscription", subject_key="self", deadline=10, monotonic=lambda: 9.75
    )
    assert result.received == 0
    assert subscriber.pulls[0]["timeout"] == 0.25
