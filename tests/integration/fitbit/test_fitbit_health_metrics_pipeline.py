"""Native Health API values survive complete transactional range replacement."""

from dataclasses import replace
from datetime import timedelta

import duckdb
import pytest

from personal_data_platform.storage.motherduck import Warehouse
from tests.fitbit_points import CASES, DAY, point, snapshot
from tests.integration.fitbit.test_fitbit_writer import apply


@pytest.fixture
def warehouse():
    value = Warehouse(duckdb.connect())
    value.migrate()
    yield value
    value.close()


@pytest.mark.parametrize("kind", CASES)
def test_native_values_survive_retry_empty_and_stale_snapshots(warehouse, kind):
    _, _, table, columns, expected = CASES[kind]
    data = snapshot(kind)
    apply(warehouse, data)
    assert warehouse.query_rows(f"SELECT {columns} FROM base.{table}") == [expected]
    assert (
        apply(warehouse, replace(data, fetched_at=data.fetched_at + timedelta(hours=1))).outcome
        == "unchanged"
    )
    empty = replace(data, records=(), fetched_at=data.fetched_at + timedelta(hours=2))
    apply(warehouse, empty)
    assert warehouse.query_value(f"SELECT count(*) FROM base.{table}") == 0
    assert apply(warehouse, data).outcome == "stale"
    apply(warehouse, replace(data, fetched_at=data.fetched_at + timedelta(hours=3)))
    assert warehouse.query_rows(f"SELECT {columns} FROM base.{table}") == [expected]
    expected_family = "google-sources" if kind == "exercise" else "google-wearables"
    assert (
        warehouse.query_value(
            "SELECT data_source_family FROM ops.fitbit_coverage WHERE data_type=?", [kind]
        )
        == f"users/me/dataSourceFamilies/{expected_family}"
    )


def test_exercise_moved_date_protects_newer_data_and_deleted_identity(warehouse):
    old = snapshot("exercise")
    field = CASES["exercise"][0]
    moved_point = point("exercise")
    for key in ("startTime", "endTime"):
        moved_point[field]["interval"][key] = moved_point[field]["interval"][key].replace(
            "09-01", "09-02"
        )
    moved = snapshot(
        "exercise",
        points=[moved_point],
        day=DAY + timedelta(days=1),
        fetched_at=old.fetched_at + timedelta(hours=2),
    )
    apply(warehouse, old)
    apply(warehouse, moved)
    apply(warehouse, replace(old, fetched_at=old.fetched_at + timedelta(hours=1)))
    assert warehouse.query_rows("SELECT source_date FROM base.fitbit_exercise") == [
        (DAY.date() + timedelta(days=1),)
    ]
    apply(warehouse, replace(moved, records=(), fetched_at=moved.fetched_at + timedelta(hours=2)))
    apply(warehouse, replace(old, fetched_at=moved.fetched_at + timedelta(hours=1)))
    assert warehouse.query_value("SELECT count(*) FROM base.fitbit_exercise") == 0


def test_missing_metrics_are_not_filled_with_zero(warehouse):
    hrv = point(
        "daily-heart-rate-variability",
        payload={"date": {"year": 2026, "month": 9, "day": 1}, "entropy": 0.0},
    )
    apply(warehouse, snapshot("daily-heart-rate-variability", points=[hrv]))
    assert warehouse.query_rows(
        "SELECT rmssd_ms, deep_sleep_rmssd_ms, entropy FROM base.fitbit_hrv_daily"
    ) == [(None, None, 0.0)]
    distance = point("distance")
    del distance["distance"]["millimeters"]
    apply(warehouse, snapshot("distance", points=[distance]))
    assert warehouse.query_value("SELECT distance_meters FROM base.fitbit_distance_interval") == 0


def test_sleep_summary_columns_preserve_missing_and_zero_values(warehouse):
    from personal_data_platform.sources.fitbit.models import Window
    from tests.unit.fitbit.test_fitbit_api import FakeTransport, client, sleep_point

    value = sleep_point()
    value["sleep"]["summary"].update(minutesInSleepPeriod="480", minutesToFallAsleep="0")
    data = client(FakeTransport([{"dataPoints": [value]}])).fetch_snapshot(
        Window("sleep", DAY, DAY + timedelta(days=1)), subject_key="self"
    )
    apply(warehouse, data)
    assert warehouse.query_rows(
        "SELECT sleep_minutes, minutes_awake, minutes_in_sleep_period, minutes_to_fall_asleep, minutes_after_wake_up FROM base.fitbit_sleep_session"
    ) == [(470, 10, 480, 0, None)]


def test_replacing_one_metric_does_not_erase_other_types(warehouse):
    from tests.integration.fitbit.test_fitbit_writer import snapshot as steps_snapshot

    apply(warehouse, steps_snapshot())
    for kind in CASES:
        apply(warehouse, snapshot(kind))
    distance = snapshot("distance")
    apply(
        warehouse,
        replace(distance, records=(), fetched_at=distance.fetched_at + timedelta(hours=1)),
    )
    for kind, (_, _, table, _, _) in CASES.items():
        assert warehouse.query_value(f"SELECT count(*) FROM base.{table}") == (
            0 if kind == "distance" else 1
        )
    assert (
        warehouse.query_value(
            "SELECT value FROM base.fitbit_activity_interval WHERE metric='steps'"
        )
        == 10
    )


@pytest.mark.parametrize("missing", ["fullSleepStats", "full", "deep", "light", "rem"])
def test_incomplete_respiratory_summary_cannot_replace_saved_measurements(warehouse, missing):
    from personal_data_platform.sources.fitbit.api import InvalidResponseError

    apply(warehouse, snapshot("respiratory-rate-sleep-summary"))
    bad = point("respiratory-rate-sleep-summary")
    payload = bad["respiratoryRateSleepSummary"]
    if missing == "fullSleepStats":
        del payload[missing]
    else:
        del payload[missing + "SleepStats"]["breathsPerMinute"]
    with pytest.raises(InvalidResponseError):
        apply(
            warehouse,
            snapshot(
                "respiratory-rate-sleep-summary", points=[bad], fetched_at=DAY + timedelta(days=5)
            ),
        )
    assert warehouse.query_rows(
        "SELECT full_breaths_per_minute, deep_breaths_per_minute FROM base.fitbit_respiratory_rate_sleep_summary"
    ) == [(14.0, 12.0)]


def test_absent_optional_respiratory_stages_stay_null(warehouse):
    value = point("respiratory-rate-sleep-summary")
    payload = value["respiratoryRateSleepSummary"]
    for stage in ("deep", "light", "rem"):
        del payload[stage + "SleepStats"]
    apply(warehouse, snapshot("respiratory-rate-sleep-summary", points=[value]))
    assert warehouse.query_rows(
        "SELECT full_breaths_per_minute, deep_breaths_per_minute, light_breaths_per_minute, rem_breaths_per_minute FROM base.fitbit_respiratory_rate_sleep_summary"
    ) == [(14.0, None, None, None)]
