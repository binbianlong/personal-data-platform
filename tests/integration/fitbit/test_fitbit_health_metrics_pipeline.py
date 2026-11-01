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
def test_native_values_survive_retry_empty_and_fresh_reacquisition(warehouse, kind):
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
    assert apply(warehouse, data).outcome == "range_replaced"
    apply(warehouse, replace(data, fetched_at=data.fetched_at + timedelta(hours=3)))
    assert warehouse.query_rows(f"SELECT {columns} FROM base.{table}") == [expected]


def test_missing_metrics_are_not_filled_with_zero(warehouse):
    hrv = point(
        "daily-heart-rate-variability",
        payload={"date": {"year": 2026, "month": 9, "day": 1}, "entropy": 0.0},
    )
    apply(warehouse, snapshot("daily-heart-rate-variability", points=[hrv]))
    assert warehouse.query_rows(
        "SELECT hrv_rmssd_ms, deep_sleep_hrv_rmssd_ms, hrv_entropy FROM base.health_daily"
    ) == [(None, None, 0.0)]
    distance = point("distance")
    del distance["distance"]["millimeters"]
    apply(warehouse, snapshot("distance", points=[distance]))
    assert warehouse.query_value("SELECT distance_meters FROM base.distance_interval") == 0


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
        "SELECT sleep_minutes, awake_minutes, sleep_period_minutes, sleep_latency_minutes, after_wake_minutes FROM base.sleep_session"
    ) == [(470, 10, 480, 0, None)]


def test_daily_group_partial_update_empty_and_all_null_removal(warehouse):
    for kind in CASES:
        apply(warehouse, snapshot(kind))
    before = warehouse.query_rows("SELECT * FROM base.health_daily")
    hrv = point(
        "daily-heart-rate-variability",
        payload={"date": {"year": 2026, "month": 9, "day": 1}, "entropy": 0.0},
    )
    apply(warehouse, snapshot("daily-heart-rate-variability", points=[hrv]))
    assert warehouse.query_rows(
        "SELECT hrv_rmssd_ms,deep_sleep_hrv_rmssd_ms,non_rem_heart_rate_bpm,hrv_entropy,respiratory_rate_breaths_per_minute FROM base.health_daily"
    ) == [(None, None, None, 0.0, 14.25)]
    for kind in CASES:
        if kind.startswith("daily-"):
            apply(warehouse, replace(snapshot(kind), records=()))
    assert warehouse.query_value("SELECT count(*) FROM base.health_daily") == 0
    assert warehouse.query_value("SELECT count(*) FROM base.exercise_session") == 1
    assert before


def test_daily_date_move_remains_until_old_day_refetched(warehouse):
    old = snapshot("daily-vo2-max")
    apply(warehouse, old)
    moved = point("daily-vo2-max")
    moved["dailyVo2Max"]["date"]["day"] = 2
    apply(warehouse, snapshot("daily-vo2-max", points=[moved], day=DAY + timedelta(days=1)))
    assert warehouse.query_rows("SELECT source_date FROM base.health_daily ORDER BY 1") == [
        (DAY.date(),),
        ((DAY + timedelta(days=1)).date(),),
    ]
    apply(warehouse, replace(old, records=()))
    assert warehouse.query_rows("SELECT source_date FROM base.health_daily") == [
        ((DAY + timedelta(days=1)).date(),)
    ]


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
        "SELECT full_breaths_per_minute, deep_breaths_per_minute FROM base.sleep_respiratory_summary"
    ) == [(14.0, 12.0)]


def test_absent_optional_respiratory_stages_stay_null(warehouse):
    value = point("respiratory-rate-sleep-summary")
    payload = value["respiratoryRateSleepSummary"]
    for stage in ("deep", "light", "rem"):
        del payload[stage + "SleepStats"]
    apply(warehouse, snapshot("respiratory-rate-sleep-summary", points=[value]))
    assert warehouse.query_rows(
        "SELECT full_breaths_per_minute, deep_breaths_per_minute, light_breaths_per_minute, rem_breaths_per_minute FROM base.sleep_respiratory_summary"
    ) == [(14.0, None, None, None)]


def test_exercise_provider_date_move_replaces_one_identity(warehouse):
    old = snapshot("exercise")
    apply(warehouse, old)
    value = point("exercise")
    for key in ("startTime", "endTime"):
        value["exercise"]["interval"][key] = value["exercise"]["interval"][key].replace(
            "09-01", "09-02"
        )
    moved = snapshot("exercise", points=[value], day=DAY + timedelta(days=1))
    apply(warehouse, moved)
    assert warehouse.query_rows(
        "SELECT exercise_session_id,source_date FROM base.exercise_session"
    ) == [("record-1", moved.records[0].source_date)]
    apply(warehouse, replace(old, records=()))
    assert warehouse.query_value("SELECT count(*) FROM base.exercise_session") == 1


def test_samples_preserve_all_stats_offsets_and_replace_only_sample_cursor(warehouse):
    value = point("respiratory-rate-sleep-summary")
    payload = value["respiratoryRateSleepSummary"]
    for stage in ("deep", "light", "rem"):
        payload[stage + "SleepStats"].update(standardDeviation=1.25, signalToNoise=-0.5)
    data = snapshot("respiratory-rate-sleep-summary", points=[value])
    apply(warehouse, data)
    assert warehouse.query_rows(
        "SELECT full_signal_to_noise,deep_standard_deviation,deep_signal_to_noise,"
        "light_standard_deviation,light_signal_to_noise,rem_standard_deviation,rem_signal_to_noise,"
        "source_date,offset_seconds FROM base.sleep_respiratory_summary"
    ) == [(8.0, 1.25, -0.5, 1.25, -0.5, 1.25, -0.5, DAY.date(), 32400)]
    from personal_data_platform.sources.fitbit.models import Window

    sample_at = data.records[0].start
    before = replace(data, window=Window(data.window.data_type, DAY, sample_at), records=())
    apply(warehouse, before)
    assert warehouse.query_value("SELECT count(*) FROM base.sleep_respiratory_summary") == 1
    at_sample = replace(
        data, window=Window(data.window.data_type, sample_at, data.window.end), records=()
    )
    apply(warehouse, at_sample)
    assert warehouse.query_value("SELECT count(*) FROM base.sleep_respiratory_summary") == 0


def test_daily_range_rollback_restores_removed_and_updated_dates(warehouse):
    from personal_data_platform.sources.fitbit.models import Window
    from personal_data_platform.sources.fitbit.motherduck import FitbitBatch

    old = snapshot("daily-vo2-max")
    apply(warehouse, old)
    next_point = point("daily-vo2-max", identity="second")
    next_point["dailyVo2Max"]["date"]["day"] = 2
    second = snapshot("daily-vo2-max", points=[next_point], day=DAY + timedelta(days=1))
    apply(warehouse, second)
    before = warehouse.query_rows("SELECT * FROM base.health_daily ORDER BY source_date")
    changed = replace(
        old,
        window=Window("daily-vo2-max", DAY, DAY + timedelta(days=2)),
        records=(replace(old.records[0], details=replace(old.records[0].details, estimated=True)),),
    )

    class FailingConnection:
        def execute(self, sql, parameters=None):
            if sql.startswith("INSERT INTO base.health_daily"):
                assert warehouse.query_value("SELECT count(*) FROM base.health_daily") == 1
                raise duckdb.ConstraintException("daily insert failed")
            return warehouse.connection.execute(sql, parameters)

    warehouse.connection.execute("BEGIN")
    try:
        with pytest.raises(duckdb.ConstraintException, match="daily insert failed"):
            FitbitBatch(changed).write_snapshot(
                FailingConnection(), source_key="unused", loaded_at=changed.fetched_at
            )
    finally:
        warehouse.connection.execute("ROLLBACK")
    assert warehouse.query_rows("SELECT * FROM base.health_daily ORDER BY source_date") == before


def test_native_scalar_api_fields_reach_separate_base_relations(warehouse):
    from tests.unit.fitbit.test_fitbit_api import fetch_points, interval, steps

    apply(warehouse, fetch_points("steps", [steps()]))
    apply(
        warehouse,
        fetch_points(
            "active-zone-minutes",
            [
                {
                    "activeZoneMinutes": {
                        "interval": interval(),
                        "heartRateZone": "CARDIO",
                        "activeZoneMinutes": "2",
                    }
                }
            ],
        ),
    )
    apply(
        warehouse,
        fetch_points(
            "daily-resting-heart-rate",
            [
                {
                    "dailyRestingHeartRate": {
                        "date": {"year": 2026, "month": 9, "day": 1},
                        "beatsPerMinute": "54",
                    }
                }
            ],
        ),
    )
    assert warehouse.query_rows(
        "SELECT start_at,end_at,source_date,steps,offset_seconds,end_offset_seconds FROM base.steps_interval"
    ) == [(DAY, DAY + timedelta(minutes=1), DAY.date(), 12, 32400, 32400)]
    assert warehouse.query_rows(
        "SELECT active_zone_minutes,category,source_date,offset_seconds,end_offset_seconds FROM base.active_zone_interval"
    ) == [(2, "cardio", DAY.date(), 32400, 32400)]
    assert warehouse.query_rows(
        "SELECT source_date,resting_heart_rate_bpm FROM base.health_daily"
    ) == [(DAY.date(), 54)]


@pytest.mark.parametrize(
    "target", ["daily-resting-heart-rate", *[kind for kind in CASES if kind.startswith("daily-")]]
)
def test_empty_daily_group_preserves_every_other_metric(warehouse, target):
    from tests.unit.fitbit.test_fitbit_api import fetch_points

    resting = fetch_points(
        "daily-resting-heart-rate",
        [
            {
                "dailyRestingHeartRate": {
                    "date": {"year": 2026, "month": 9, "day": 1},
                    "beatsPerMinute": "54",
                }
            }
        ],
    )
    values = {kind: snapshot(kind) for kind in CASES if kind.startswith("daily-")}
    values["daily-resting-heart-rate"] = resting
    for data in values.values():
        apply(warehouse, data)
    before = warehouse.query_rows("SELECT * FROM base.health_daily")
    apply(warehouse, replace(values[target], records=()))
    for kind, (_, _, table, columns, expected) in CASES.items():
        if kind.startswith("daily-"):
            assert warehouse.query_rows(f"SELECT {columns} FROM base.{table}") == [
                tuple(None for _ in expected) if kind == target else expected
            ]
    assert warehouse.query_value("SELECT resting_heart_rate_bpm FROM base.health_daily") == (
        None if target == "daily-resting-heart-rate" else 54
    )
    assert before
