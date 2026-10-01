"""Authentication and range coverage at the Google Health boundary."""

import base64
import json
from datetime import UTC, datetime

import pytest

from personal_data_platform.sources.fitbit.models import Window
from personal_data_platform.sources.fitbit.webhook import (
    AuthenticationError,
    GoogleHealthAuthenticator,
    PayloadError,
    Verification,
    VerifiedNotification,
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
    value = notification()
    del value["data"]["intervals"][0]["physicalTimeInterval"]
    result = authenticate(value)
    assert result.windows == (
        Window("steps", time("2026-09-26T05:00:00"), time("2026-09-28T01:00:00")),
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
