"""Authentication and range coverage at the Google Health boundary."""

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from personal_data_platform.sources.fitbit.models import Window
from personal_data_platform.sources.fitbit.webhook import (
    AuthenticationError,
    GoogleHealthAuthenticator,
    PayloadError,
    Verification,
    VerifiedNotification,
    split_notifications,
)


def time(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


class Signature:
    calls: list[tuple[bytes, bytes]]

    def __init__(self) -> None:
        self.calls = []

    def verify(self, signature: bytes, payload: bytes) -> bool:
        self.calls.append((signature, payload))
        return signature == b"signed"


def notification(kind: str = "steps", *, owner: str = "health-owner") -> dict:
    return {
        "data": {
            "version": "1",
            "healthUserId": owner,
            "operation": "UPSERT",
            "dataType": kind,
            "intervals": [
                {
                    "physicalTimeInterval": {
                        "startTime": "2026-09-26T14:00:00Z",
                        "endTime": "2026-09-26T22:00:00Z",
                    },
                    "civilIso8601TimeInterval": {
                        "startTime": "2026-09-26T23:00:00",
                        "endTime": "2026-09-27T07:00:00",
                    },
                }
            ],
        }
    }


def authenticate(payload: object, *, verifier: Signature | None = None, **kwargs: object):
    authenticator = GoogleHealthAuthenticator(
        authorization="Bearer secret",
        health_user_id="health-owner",
        subject_key="subject",
        signatures=verifier or Signature(),
    )
    options = {
        "authorization": "Bearer secret",
        "signature_header": base64.b64encode(b"signed").decode(),
        "content_type": "application/json; charset=utf-8",
        "body": json.dumps(payload).encode(),
        **kwargs,
    }
    return authenticator.authenticate(**options)


def test_authorized_verification_needs_no_signature() -> None:
    verifier = Signature()
    assert isinstance(
        authenticate({"type": "verification"}, verifier=verifier, signature_header=None),
        Verification,
    )
    assert verifier.calls == []
    with pytest.raises(AuthenticationError):
        authenticate({"type": "verification"}, authorization=None)


@pytest.mark.parametrize("signature", [None, "!", base64.b64encode(b"wrong").decode()])
def test_notifications_require_signature_of_unchanged_body(signature: str | None) -> None:
    with pytest.raises(AuthenticationError):
        authenticate(notification(), signature_header=signature)


def test_whole_batch_must_match_owner_and_five_types() -> None:
    for invalid in [notification(owner="other"), notification("weight"), {"data": {}}]:
        with pytest.raises((AuthenticationError, PayloadError)):
            authenticate([notification(), invalid])


def test_batch_merges_same_kind_and_does_not_store_private_identifier() -> None:
    verifier = Signature()
    later = notification()
    later["data"]["intervals"][0]["physicalTimeInterval"]["endTime"] = "2026-09-27T00:00:00Z"
    result = authenticate([notification(), later, notification("heart-rate")], verifier=verifier)
    assert isinstance(result, VerifiedNotification)
    assert result.subject_key == "subject"
    assert result.windows == (
        Window("steps", time("2026-09-26T14:00:00"), time("2026-09-27T00:00:00")),
        Window("heart-rate", time("2026-09-26T14:00:00"), time("2026-09-26T22:00:00")),
    )
    assert len(verifier.calls) == 1
    assert "health-owner" not in repr(result)


def test_sleep_covers_civil_end_date_including_midnight_end() -> None:
    value = notification("sleep")
    value["data"]["intervals"][0]["civilIso8601TimeInterval"]["endTime"] = "2026-09-27T00:00:00"
    result = authenticate(value)
    assert result.windows == (
        Window("sleep", time("2026-09-26T00:00:00"), time("2026-09-28T00:00:00")),
    )


def test_daily_metric_uses_civil_dates_without_physical_time() -> None:
    value = notification("daily-resting-heart-rate")
    interval = value["data"]["intervals"][0]
    del interval["physicalTimeInterval"]
    interval["civilIso8601TimeInterval"] = {
        "startTime": "2026-09-26T00:00:00",
        "endTime": "2026-09-27T00:00:00",
    }
    result = authenticate(value)
    assert result.windows == (
        Window(
            "daily-resting-heart-rate", time("2026-09-26T00:00:00"), time("2026-09-27T00:00:00")
        ),
    )


def test_civil_only_physical_metric_covers_all_legal_utc_offsets() -> None:
    value = notification("active-zone-minutes")
    del value["data"]["intervals"][0]["physicalTimeInterval"]
    result = authenticate(value)
    assert result.windows == (
        Window("active-zone-minutes", time("2026-09-26T05:00:00"), time("2026-09-28T01:00:00")),
    )


@pytest.mark.parametrize("change", ["empty", "reverse", "naive", "operation", "version"])
def test_invalid_notifications_fail_before_returning_a_batch(change: str) -> None:
    value = notification()
    if change == "empty":
        value["data"]["intervals"] = []
    elif change == "reverse":
        value["data"]["intervals"][0]["physicalTimeInterval"]["endTime"] = "2026-09-25T22:00:00Z"
    elif change == "naive":
        value["data"]["intervals"][0]["physicalTimeInterval"]["endTime"] = "2026-09-26T22:00:00"
    else:
        value["data"][change] = "unknown"
    with pytest.raises(PayloadError):
        authenticate([notification(), value])


def test_notification_splits_across_tokyo_days():
    from personal_data_platform.sources.fitbit import webhook
    from personal_data_platform.sources.fitbit.models import Notification

    assert hasattr(webhook, "split_notifications")
    parent = Notification(
        "n" * 128,
        "self",
        (Window("steps", time("2026-10-01T14:00:00"), time("2026-10-02T16:00:00")),),
        time("2026-10-04T00:00:00"),
    )
    units = webhook.split_notifications(parent)
    assert [unit.windows for unit in units] == [
        (Window("steps", time("2026-10-01T14:00:00"), time("2026-10-01T15:00:00")),),
        (Window("steps", time("2026-10-01T15:00:00"), time("2026-10-02T15:00:00")),),
        (Window("steps", time("2026-10-02T15:00:00"), time("2026-10-02T16:00:00")),),
    ]
    assert units == webhook.split_notifications(parent)
    assert len({unit.notification_id for unit in units}) == 3
    assert all(len(unit.notification_id) <= 128 for unit in units)


def test_split_sleep_uses_provider_dates_and_conservative_physical_excludes_future_days():
    from personal_data_platform.sources.fitbit import webhook
    from personal_data_platform.sources.fitbit.models import Notification

    assert hasattr(webhook, "split_notifications")
    sleep = Notification(
        "s",
        "self",
        (Window("sleep", time("2026-09-26T00:00:00"), time("2026-09-28T00:00:00")),),
        time("2026-09-29T00:00:00"),
    )
    assert [u.windows[0] for u in webhook.split_notifications(sleep)] == [
        Window("sleep", time("2026-09-26T00:00:00"), time("2026-09-27T00:00:00")),
        Window("sleep", time("2026-09-27T00:00:00"), time("2026-09-28T00:00:00")),
    ]
    value = notification("active-zone-minutes")
    del value["data"]["intervals"][0]["physicalTimeInterval"]
    verified = authenticate(value)
    units = webhook.split_notifications(
        Notification("c", "subject", verified.windows, time("2026-09-27T00:00:00"))
    )
    assert [u.windows[0] for u in units] == [
        Window("active-zone-minutes", time("2026-09-26T05:00:00"), time("2026-09-26T15:00:00")),
        Window("active-zone-minutes", time("2026-09-26T15:00:00"), time("2026-09-27T15:00:00")),
    ]


def test_split_rejects_future_only_physical_notification():
    from personal_data_platform.sources.fitbit import webhook
    from personal_data_platform.sources.fitbit.models import Notification

    assert hasattr(webhook, "split_notifications")
    with pytest.raises(PayloadError):
        webhook.split_notifications(
            Notification(
                "f",
                "self",
                (Window("steps", time("2026-10-02T00:00:00"), time("2026-10-03T00:00:00")),),
                time("2026-10-01T00:00:00"),
            )
        )


def test_notification_observations_preserve_source_intervals_without_private_fields():
    value = notification("heart-rate")
    data = value["data"]
    data["recordId"] = "record-42"
    data["unknown"] = "private-extra"
    data["intervals"][0]["physicalTimeInterval"]["startTime"] = "2026-09-26T14:00:20Z"
    data["intervals"][0]["civilIso8601TimeInterval"]["startTime"] = "2026-09-26T23:00:20"
    result = authenticate(value)
    observations = getattr(result, "observations", ())
    assert len(observations) == 1
    observation = observations[0]
    assert observation.data_type == "heart-rate"
    assert observation.operation == "UPSERT"
    assert observation.record_id == "record-42"
    assert observation.intervals[0].physical_start == "2026-09-26T14:00:20+00:00"
    assert observation.intervals[0].physical_end == "2026-09-26T22:00:00+00:00"
    assert observation.intervals[0].civil_start == "2026-09-26T23:00:20"
    assert observation.intervals[0].civil_end == "2026-09-27T07:00:00"
    assert "health-owner" not in repr(result)
    assert "private-extra" not in repr(result)
    assert len(observation.sha256()) == 64
    data["unknown"] = "different-extra"
    assert authenticate(value).observations[0].sha256() == observation.sha256()


def test_notification_observations_keep_delete_and_civil_only_intervals():
    value = notification("sleep")
    data = value["data"]
    data["operation"] = "DELETE"
    data["recordId"] = "sleep-42"
    del data["intervals"][0]["physicalTimeInterval"]
    result = authenticate(value)
    observations = getattr(result, "observations", ())
    assert len(observations) == 1
    assert observations[0].operation == "DELETE"
    assert observations[0].record_id == "sleep-42"
    assert observations[0].intervals[0].physical_start is None
    assert observations[0].intervals[0].physical_end is None
    assert observations[0].intervals[0].civil_start == "2026-09-26T23:00:00"
    assert observations[0].intervals[0].civil_end == "2026-09-27T07:00:00"


@pytest.mark.parametrize(
    "start,end,expected_start,expected_end",
    [
        ("14:00:20Z", "14:01:10Z", "14:00:00", "14:02:00"),
        ("14:00:00Z", "14:01:00Z", "14:00:00", "14:01:00"),
        ("14:00:59.999999999Z", "14:01:00Z", "14:00:00", "14:01:00"),
        ("14:00:00Z", "14:01:00.000000001Z", "14:00:00", "14:02:00"),
        ("14:00:00.000000001Z", "14:00:00.000000002Z", "14:00:00", "14:01:00"),
        ("23:00:20+09:00", "23:01:00.000000999+09:00", "14:00:00", "14:02:00"),
    ],
)
def test_physical_heart_rate_rounds_outward_without_losing_nanoseconds(
    start, end, expected_start, expected_end
):
    value = notification("heart-rate")
    value["data"]["intervals"][0]["physicalTimeInterval"] = {
        "startTime": "2026-09-26T" + start,
        "endTime": "2026-09-26T" + end,
    }
    result = authenticate(value)
    assert result.groups == (
        (
            Window(
                "heart-rate",
                time("2026-09-26T" + expected_start),
                time("2026-09-26T" + expected_end),
            ),
        ),
    )
    assert result.group_modes == ("bounded_minutes",)


def test_nanosecond_heart_rate_observation_retains_unrounded_bounds():
    value = notification("heart-rate")
    value["data"]["intervals"][0]["physicalTimeInterval"] = {
        "startTime": "2026-09-26T14:00:20.000000001Z",
        "endTime": "2026-09-26T14:01:00.000000999Z",
    }
    observed = authenticate(value).observations[0].intervals[0]
    assert observed.physical_start == "2026-09-26T14:00:20.000000001+00:00"
    assert observed.physical_end == "2026-09-26T14:01:00.000000999+00:00"


@pytest.mark.parametrize("end", ["14:00:00.000000001Z", "14:00:00.000000000Z"])
def test_heart_rate_rejects_nonpositive_raw_range_before_rounding(end):
    value = notification("heart-rate")
    value["data"]["intervals"][0]["physicalTimeInterval"] = {
        "startTime": "2026-09-26T14:00:00.000000001Z",
        "endTime": "2026-09-26T" + end,
    }
    with pytest.raises(PayloadError):
        authenticate(value)


def test_civil_only_and_mixed_heart_rate_retain_legacy_acquisition():
    for mixed in (False, True):
        value = notification("heart-rate")
        if mixed:
            value["data"]["intervals"].append(dict(value["data"]["intervals"][0]))
        del value["data"]["intervals"][0]["physicalTimeInterval"]
        result = authenticate(value)
        assert result.group_modes == ("legacy",)
        assert result.groups[0] == (
            Window("heart-rate", time("2026-09-26T05:00:00"), time("2026-09-28T01:00:00")),
        )


def test_bounded_heart_rate_split_preserves_mode_and_tokyo_boundary():
    from personal_data_platform.sources.fitbit.models import Notification
    from personal_data_platform.sources.fitbit.webhook import split_notifications

    parent = Notification(
        "bounded",
        "self",
        (Window("heart-rate", time("2026-09-26T14:59:00"), time("2026-09-26T15:01:00")),),
        time("2026-09-27T00:00:00"),
        acquisition_mode="bounded_minutes",
    )
    units = split_notifications(parent)
    assert [u.windows[0] for u in units] == [
        Window("heart-rate", time("2026-09-26T14:59:00"), time("2026-09-26T15:00:00")),
        Window("heart-rate", time("2026-09-26T15:00:00"), time("2026-09-26T15:01:00")),
    ]
    assert all(u.acquisition_mode == "bounded_minutes" for u in units)


def test_bounded_heart_rate_split_keeps_unfinished_minute_across_tokyo_midnight():
    from personal_data_platform.sources.fitbit.models import Notification
    from personal_data_platform.sources.fitbit.webhook import split_notifications

    parent = Notification(
        "tail",
        "self",
        (Window("heart-rate", time("2026-09-26T14:59:00"), time("2026-09-26T15:01:00")),),
        time("2026-09-26T14:59:50"),
        acquisition_mode="bounded_minutes",
    )
    units = split_notifications(parent)
    assert [u.windows[0] for u in units] == [
        Window("heart-rate", time("2026-09-26T14:59:00"), time("2026-09-26T15:00:00")),
        Window("heart-rate", time("2026-09-26T15:00:00"), time("2026-09-26T15:01:00")),
    ]


def test_steps_partial_notification_keeps_exact_microsecond_envelope():
    payload = notification("steps")
    payload["data"]["intervals"][0]["physicalTimeInterval"] = {
        "startTime": "2026-10-01T00:00:00.000000001Z",
        "endTime": "2026-10-01T00:00:00.000000002Z",
    }
    result = authenticate(payload)
    assert result.group_modes == ("bounded_steps",)
    assert result.windows == (
        Window("steps", time("2026-10-01T00:00:00"), time("2026-10-01T00:00:00.000001")),
    )


def test_steps_full_day_notification_uses_only_provider_date():
    payload = notification("steps")
    payload["data"]["intervals"] = [
        {
            "civilIso8601TimeInterval": {
                "startTime": "2026-10-01T00:00:00",
                "endTime": "2026-10-02T00:00:00",
            },
        }
    ]
    result = authenticate(payload)
    assert result.group_modes == ("bounded_steps",)
    assert result.windows == (
        Window(
            "steps", time("2026-10-01T00:00:00"), time("2026-10-02T00:00:00"), time_basis="civil"
        ),
    )


def test_mixed_steps_windows_keep_utc_and_civil_separate():
    payload = notification("steps")
    payload["data"]["intervals"] = [
        {
            "physicalTimeInterval": {
                "startTime": "2026-10-01T00:00:00Z",
                "endTime": "2026-10-02T00:00:00Z",
            }
        },
        {
            "civilIso8601TimeInterval": {
                "startTime": "2026-10-01T00:00:00",
                "endTime": "2026-10-02T00:00:00",
            }
        },
    ]
    result = authenticate(payload)
    assert len(result.groups[0]) == 2
    assert {w.time_basis for w in result.groups[0]} == {"utc", "civil"}


def test_split_civil_steps_does_not_interpret_provider_date_as_future_utc():
    from personal_data_platform.sources.fitbit.models import Notification

    at = time("2026-10-01T00:00:00")
    notice = Notification(
        "future-calendar",
        "self",
        (Window("steps", at, at + timedelta(days=1), time_basis="civil"),),
        at - timedelta(hours=4),
        acquisition_mode="bounded_steps",
    )
    units = split_notifications(notice)
    assert len(units) == 1
    assert units[0].windows == notice.windows
