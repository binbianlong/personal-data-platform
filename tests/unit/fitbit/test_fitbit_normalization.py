"""Source-point identities, timestamps, values and sleep details."""

from datetime import UTC, date, datetime, timedelta

import pytest

from personal_data_platform.sources.fitbit.normalization import normalize
from tests.fitbit_points import DAY as START
from tests.fitbit_points import interval, sleep_point, steps


def test_steps_keep_physical_start_cursor_and_both_offsets_and_provider_date():
    point = steps("15", start="2026-09-01T15:59:00Z", end="2026-09-01T16:01:00Z")
    point["steps"]["interval"].update(
        {
            "startUtcOffset": "28800s",
            "endUtcOffset": "32400s",
            "civilStartTime": {
                "date": {"year": 2026, "month": 9, "day": 1},
                "time": {"hours": 23, "minutes": 59},
            },
        }
    )
    (record,) = normalize("steps", point, "owner")
    assert record.start == datetime(2026, 9, 1, 15, 59, tzinfo=UTC)
    assert record.cursor == record.start
    assert record.end == datetime(2026, 9, 1, 16, 1, tzinfo=UTC)
    assert (record.offset_seconds, record.end_offset_seconds) == (28800, 32400)
    assert record.source_date == date(2026, 9, 1)


def test_steps_key_uses_interval_and_subject_and_does_not_change_with_value():
    (original,) = normalize("steps", steps(), "owner")
    (changed,) = normalize("steps", steps(count="65"), "owner")
    (other,) = normalize("steps", steps(), "other")
    assert original.cursor == original.start == START
    assert original.end == START + timedelta(minutes=1)
    assert original.record_id == changed.record_id
    assert original.record_id != other.record_id
    assert original.value == 12


def test_resting_heart_rate_uses_civil_date_not_a_timezone_shifted_instant():
    point = {
        "dailyRestingHeartRate": {
            "date": {"year": 2026, "month": 9, "day": 1},
            "beatsPerMinute": "54",
        }
    }
    (record,) = normalize("daily-resting-heart-rate", point, "owner")
    assert record.cursor == record.start == START
    assert record.source_date == date(2026, 9, 1)
    assert record.offset_seconds is None
    assert record.value == 54


def test_active_zone_minutes_are_already_weighted():
    point = {
        "activeZoneMinutes": {
            "interval": interval(),
            "heartRateZone": "CARDIO",
            "activeZoneMinutes": "2",
        }
    }
    (record,) = normalize("active-zone-minutes", point, "owner")
    assert record.value == 2
    assert record.category == "cardio"


def test_sleep_accepts_consistent_names_used_in_the_official_filter_example():
    point = sleep_point()
    point["name"] = point.pop("dataPointName")
    metadata = point["sleep"]["metadata"]
    metadata["main"] = metadata.pop("mainSleep")
    result = normalize("sleep", point, "owner")
    assert result[0].record_id == "sleep-001"
    assert result[0].is_main_sleep is True


@pytest.mark.parametrize("conflict", ["identity", "main"])
def test_sleep_rejects_conflicting_alias_values(conflict):
    point = sleep_point()
    if conflict == "identity":
        point["name"] = "users/synthetic-user/dataTypes/sleep/dataPoints/other"
    else:
        point["sleep"]["metadata"]["main"] = False
    with pytest.raises(ValueError):
        normalize("sleep", point, "owner")


def test_sleep_preserves_end_civil_date_and_keeps_overlapping_wakes_separate():
    result = normalize("sleep", sleep_point(), "owner")
    session = result[0]
    assert session.record_id == "sleep-001"
    assert session.cursor == START
    assert session.source_date == date(2026, 9, 1)
    assert session.start == datetime(2026, 8, 31, 14, tzinfo=UTC)
    assert session.value == 470
    assert session.is_main_sleep is True
    stages = [record for record in result if record.kind == "sleep-stage"]
    wakes = [record for record in result if record.kind == "sleep-wake"]
    assert [(row.category, row.value) for row in stages] == [("light", 14400), ("deep", 14400)]
    assert [(row.category, row.value) for row in wakes] == [
        ("short-awakening", 30),
        ("out-of-bed", 60),
    ]
    assert all(
        row.cursor == session.cursor and row.parent_id == "sleep-001" for row in (*stages, *wakes)
    )


def test_missing_sleep_summary_and_main_classification_stay_unknown():
    point = sleep_point()
    del point["sleep"]["summary"]
    point["sleep"]["metadata"] = {"nap": False}
    session = normalize("sleep", point, "owner")[0]
    assert session.value is None
    assert session.is_main_sleep is None


def test_sleep_civil_end_with_changed_offset_controls_replacement_day():
    point = sleep_point()
    point["sleep"]["interval"].update(
        {
            "startUtcOffset": "28800s",
            "endUtcOffset": "32400s",
            "civilEndTime": {"date": {"year": 2026, "month": 9, "day": 1}, "time": {"hours": 7}},
        }
    )
    session = normalize("sleep", point, "owner")[0]
    assert (session.offset_seconds, session.end_offset_seconds) == (28800, 32400)
    assert session.cursor == START
