"""Native metrics produce one daily row without inventing missing values."""

from datetime import date, timedelta

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.sources.fitbit.models import Record, Snapshot, Window
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.fitbit_points import CASES, DAY, point, snapshot
from tests.integration.fitbit.test_fitbit_writer import apply
from tests.unit.fitbit.test_fitbit_api import FakeTransport, client, sleep_point


def test_daily_and_session_views_keep_native_values_and_day_boundaries(
    tmp_path, monkeypatch, dbt_project
):
    database = tmp_path / "health.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    warehouse.migrate()
    for kind in CASES:
        apply(warehouse, snapshot(kind))
    # Multiple samples must not multiply daily provider summaries or create extra dates.
    for kind in ("heart-rate-variability", "respiratory-rate-sleep-summary"):
        extra = point(kind, identity="extra-sample")
        extra[CASES[kind][0]]["sampleTime"]["physicalTime"] = "2026-09-04T01:02:03Z"
        apply(warehouse, snapshot(kind, points=[extra], day=DAY + timedelta(days=3)))
    unknown = point("time-in-heart-rate-zone", identity="future-zone")
    unknown["timeInHeartRateZone"]["heartRateZoneType"] = "FUTURE_ZONE"
    unknown["timeInHeartRateZone"]["interval"].update(
        startTime="2026-09-03T01:00:00Z", endTime="2026-09-03T01:02:00Z"
    )
    apply(
        warehouse,
        snapshot("time-in-heart-rate-zone", points=[unknown], day=DAY + timedelta(days=2)),
    )
    no_baseline = point(
        "daily-sleep-temperature-derivations",
        payload={
            "date": {"year": 2026, "month": 9, "day": 2},
            "nightlyTemperatureCelsius": 34.5,
        },
        identity="no-baseline",
    )
    apply(
        warehouse,
        snapshot(
            "daily-sleep-temperature-derivations", points=[no_baseline], day=DAY + timedelta(days=1)
        ),
    )
    azm = Record(
        "active-zone-minutes",
        "azm",
        DAY + timedelta(hours=14, minutes=59),
        DAY + timedelta(hours=14, minutes=59),
        DAY + timedelta(hours=15, minutes=1),
        4,
        category="cardio",
    )
    apply(
        warehouse,
        Snapshot(
            "self",
            Window("active-zone-minutes", DAY, DAY + timedelta(days=1)),
            DAY + timedelta(days=4),
            (azm,),
        ),
    )
    value = sleep_point()
    value["sleep"]["summary"].update(
        minutesInSleepPeriod="480", minutesToFallAsleep="0", minutesAfterWakeUp="2"
    )
    apply(
        warehouse,
        client(FakeTransport([{"dataPoints": [value]}])).fetch_snapshot(
            Window("sleep", DAY, DAY + timedelta(days=1)), subject_key="self"
        ),
    )
    # Provider start date differs from the JST analysis date when travelling.
    travel = point("exercise", identity="travel")
    timing = travel["exercise"]["interval"]
    timing.update(
        startTime="2026-09-01T01:00:00Z",
        endTime="2026-09-01T01:02:00Z",
        startUtcOffset="-28800s",
        endUtcOffset="-28800s",
    )
    del travel["exercise"]["activeDuration"]
    apply(warehouse, snapshot("exercise", points=[travel], day=DAY - timedelta(days=1)))
    warehouse.close()
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project, selector="tag:fitbit")
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_rows(
            "SELECT activity_date, distance_meters, sedentary_seconds, heart_rate_zone_moderate_seconds, "
            "heart_rate_zone_light_seconds, heart_rate_zone_vigorous_seconds, heart_rate_zone_peak_seconds, "
            "active_zone_minutes FROM marts.daily_fitbit_health ORDER BY activity_date"
        ) == [
            (date(2026, 9, 1), 60.0, 60.0, 60.0, None, None, None, 2.0),
            (date(2026, 9, 2), 60.0, 60.0, 60.0, None, None, None, 2.0),
            (date(2026, 9, 3), None, None, None, None, None, None, None),
        ]
        assert warehouse.query_rows(
            "SELECT hrv_rmssd_ms, deep_sleep_hrv_rmssd_ms, respiratory_rate_breaths_per_minute, "
            "spo2_average_percent, spo2_lower_bound_percent, spo2_upper_bound_percent, spo2_standard_deviation_percent, "
            "sleep_temperature_delta_celsius, vo2_max_ml_kg_min, vo2_max_estimated "
            "FROM marts.daily_fitbit_health ORDER BY activity_date"
        ) == [
            (42.5, 51.0, 14.25, 97.5, 96.0, 99.0, 0.75, -0.25, 48.5, False),
            (None,) * 10,
            (None,) * 10,
        ]
        assert warehouse.query_rows(
            "SELECT minutes_awake, minutes_in_sleep_period, minutes_to_fall_asleep, minutes_after_wake_up FROM marts.fitbit_sleep_sessions"
        ) == [(10, 480, 0, 2)]
        assert warehouse.query_rows(
            "SELECT exercise_id, activity_date, source_date, exercise_type, active_duration_seconds, elapsed_seconds "
            "FROM marts.fitbit_exercise_sessions ORDER BY exercise_id"
        ) == [
            ("record-1", date(2026, 9, 1), date(2026, 9, 1), "NEW_EXERCISE", 90.5, 120.0),
            ("travel", date(2026, 9, 1), date(2026, 8, 31), "NEW_EXERCISE", None, 120.0),
        ]
    finally:
        warehouse.close()
