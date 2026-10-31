"""Offline request/normalization contracts for additional Google Health types."""

from copy import deepcopy
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from personal_data_platform.sources.fitbit.api import InvalidResponseError
from personal_data_platform.sources.fitbit.models import Window
from tests.fitbit_points import CASES, DAY, point, snapshot
from tests.unit.fitbit.test_fitbit_api import FakeTransport, client


@pytest.mark.parametrize("kind", CASES)
def test_requests_use_native_cursor_source_and_all_pages(kind):
    transport = FakeTransport([{"nextPageToken": "next"}, {"dataPoints": [point(kind)]}])
    data = client(transport).fetch_snapshot(
        Window(kind, DAY, DAY + timedelta(days=1)), subject_key="self"
    )
    assert len(data.records) == 1
    queries = [parse_qs(urlsplit(call[1]).query) for call in transport.calls]
    prefix = kind.replace("-", "_")
    if kind.startswith("daily-"):
        field, lower, upper = prefix + ".date", "2026-09-01", "2026-09-02"
    elif kind == "exercise":
        field, lower, upper = "exercise.interval.civil_start_time", "2026-09-01", "2026-09-02"
    elif kind in ("heart-rate-variability", "respiratory-rate-sleep-summary"):
        field, lower, upper = (
            prefix + ".sample_time.physical_time",
            "2026-09-01T00:00:00Z",
            "2026-09-02T00:00:00Z",
        )
        assert data.records[0].end is None
    else:
        field, lower, upper = (
            prefix + ".interval.start_time",
            "2026-09-01T00:00:00Z",
            "2026-09-02T00:00:00Z",
        )
    assert queries[0]["filter"] == [f'{field} >= "{lower}" AND {field} < "{upper}"']
    family = "google-sources" if kind == "exercise" else "google-wearables"
    assert queries[0]["dataSourceFamily"] == [f"users/me/dataSourceFamilies/{family}"]
    assert queries[0]["pageSize"] == ["25" if kind == "exercise" else "10000"]
    assert queries[1]["pageToken"] == ["next"]


@pytest.mark.parametrize(
    "kind, field, value",
    [
        ("daily-respiratory-rate", "breathsPerMinute", True),
        ("daily-oxygen-saturation", "averagePercentage", 101),
        ("daily-sleep-temperature-derivations", "nightlyTemperatureCelsius", float("nan")),
        ("daily-vo2-max", "estimated", "false"),
        ("daily-vo2-max", "vo2Max", -1),
        ("heart-rate-variability", "standardDeviationMilliseconds", -1),
        ("exercise", "activeDuration", "-1s"),
        ("distance", "millimeters", True),
    ],
)
def test_malformed_metrics_never_produce_a_replacement_snapshot(kind, field, value):
    bad = point(kind)
    bad[CASES[kind][0]][field] = value
    with pytest.raises(InvalidResponseError):
        snapshot(kind, points=[bad])


def test_duplicate_provider_dates_with_distinct_ids_are_rejected():
    with pytest.raises(InvalidResponseError):
        snapshot(
            "daily-vo2-max",
            points=[point("daily-vo2-max"), point("daily-vo2-max", identity="other")],
        )


def test_missing_ids_are_stable_across_value_corrections_and_zones_stay_distinct():
    first = point("heart-rate-variability")
    del first["dataPointName"]
    correction = deepcopy(first)
    correction["heartRateVariability"]["standardDeviationMilliseconds"] = 36.5
    assert (
        snapshot("heart-rate-variability", points=[first]).records[0].record_id
        == snapshot("heart-rate-variability", points=[correction]).records[0].record_id
    )
    zone = point("time-in-heart-rate-zone")
    del zone["dataPointName"]
    other_zone = deepcopy(zone)
    other_zone["timeInHeartRateZone"]["heartRateZoneType"] = "FUTURE_ZONE"
    records = snapshot("time-in-heart-rate-zone", points=[zone, other_zone]).records
    assert len({r.record_id for r in records}) == 2
    assert {r.category for r in records} == {"MODERATE", "FUTURE_ZONE"}


def test_exercise_cursor_uses_provider_start_date_across_midnight():
    value = point("exercise")
    timing = value["exercise"]["interval"]
    timing["startTime"], timing["endTime"] = "2026-08-31T23:59:00Z", "2026-09-01T00:01:00Z"
    data = snapshot("exercise", points=[value])
    assert data.records[0].cursor == DAY
    assert data.records[0].source_date == DAY.date()


def test_sleep_summary_keeps_zero_and_null_without_reconstructing_from_stages():
    from tests.unit.fitbit.test_fitbit_api import sleep_point

    value = sleep_point()
    value["sleep"]["summary"].update(
        minutesAwake="17", minutesInSleepPeriod="420", minutesToFallAsleep="0"
    )
    transport = FakeTransport([{"dataPoints": [value]}])
    data = client(transport).fetch_snapshot(
        Window("sleep", DAY, DAY + timedelta(days=1)), subject_key="self"
    )
    sleep = next(r for r in data.records if r.kind == "sleep")
    assert sleep.details.minutes_awake == 17
    assert sleep.details.minutes_in_sleep_period == 420
    assert sleep.details.minutes_to_fall_asleep == 0
    assert sleep.details.minutes_after_wake_up is None


@pytest.mark.parametrize("identity_field", ["dataPointName", "name"])
def test_respiratory_collection_identity_uses_stable_missing_id_fallback(identity_field):
    kind = "respiratory-rate-sleep-summary"
    collection = point(kind)
    del collection["dataPointName"]
    missing = deepcopy(collection)
    collection[identity_field] = f"users/synthetic-user/dataTypes/{kind}/dataPoints/"
    expected = snapshot(kind, points=[missing]).records[0]
    assert snapshot(kind, points=[collection]).records[0] == expected
    for empty in [None, ""]:
        unnamed = deepcopy(missing)
        unnamed[identity_field] = empty
        assert snapshot(kind, points=[unnamed]).records[0] == expected
    corrected = deepcopy(collection)
    corrected["respiratoryRateSleepSummary"]["fullSleepStats"]["breathsPerMinute"] = 15.5
    assert snapshot(kind, points=[corrected]).records[0].record_id == expected.record_id
    assert (
        snapshot(kind, points=[point(kind, identity="provider-id")]).records[0].record_id
        == "provider-id"
    )
    with pytest.raises(InvalidResponseError, match="duplicate identity"):
        snapshot(kind, points=[collection, missing])


@pytest.mark.parametrize(
    "name",
    [
        "users//dataTypes/respiratory-rate-sleep-summary/dataPoints/",
        "users/synthetic-user/dataTypes/heart-rate-variability/dataPoints/",
        "users/synthetic-user/dataTypes/respiratory-rate-sleep-summary/dataPoints//",
        "users/synthetic-user/dataTypes/respiratory-rate-sleep-summary/dataPoints/id/extra",
        "users/synthetic-user/dataTypes/respiratory-rate-sleep-summary/dataPoints",
        "unexpected/users/synthetic-user/dataTypes/respiratory-rate-sleep-summary/dataPoints/",
    ],
)
def test_respiratory_collection_identity_rejects_malformed_resource_paths(name):
    value = point("respiratory-rate-sleep-summary")
    value["dataPointName"] = name
    with pytest.raises(InvalidResponseError):
        snapshot("respiratory-rate-sleep-summary", points=[value])


def test_respiratory_collection_identity_preserves_conflicting_alias_rejection():
    kind = "respiratory-rate-sleep-summary"
    value = point(kind)
    value["name"] = f"users/synthetic-user/dataTypes/{kind}/dataPoints/"
    with pytest.raises(InvalidResponseError):
        snapshot(kind, points=[value])


@pytest.mark.parametrize(
    "kind", ["heart-rate-variability", "daily-respiratory-rate", "exercise", "sleep"]
)
def test_respiratory_collection_identity_exception_does_not_apply_to_other_types(kind):
    from tests.unit.fitbit.test_fitbit_api import sleep_point

    value = sleep_point() if kind == "sleep" else point(kind)
    value["dataPointName"] = f"users/synthetic-user/dataTypes/{kind}/dataPoints/"
    with pytest.raises(InvalidResponseError):
        snapshot(kind, points=[value])
