"""Synthetic Google Health records and independently specified stored values."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from tests.unit.fitbit.test_fitbit_api import FakeTransport, client, interval

DAY = datetime(2026, 9, 1, tzinfo=UTC)
DATE = {"year": 2026, "month": 9, "day": 1}
SAMPLE_TIME = {"physicalTime": "2026-09-01T01:02:03Z", "utcOffset": "32400s"}
TIMING = interval(start="2026-09-01T14:59:00Z", end="2026-09-01T15:01:00Z")

# data type: JSON union field, payload, table, projected columns, expected row
CASES = {
    "daily-heart-rate-variability": (
        "dailyHeartRateVariability",
        {
            "date": DATE,
            "averageHeartRateVariabilityMilliseconds": 42.5,
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 51.0,
            "nonRemHeartRateBeatsPerMinute": "58",
            "entropy": 2.1,
        },
        "fitbit_hrv_daily",
        "rmssd_ms, deep_sleep_rmssd_ms, non_rem_heart_rate_bpm, entropy",
        (42.5, 51.0, 58, 2.1),
    ),
    "heart-rate-variability": (
        "heartRateVariability",
        {
            "sampleTime": SAMPLE_TIME,
            "rootMeanSquareOfSuccessiveDifferencesMilliseconds": 43.0,
            "standardDeviationMilliseconds": 35.5,
        },
        "fitbit_hrv_sample",
        "sample_at, rmssd_ms, sdnn_ms",
        (datetime(2026, 9, 1, 1, 2, 3, tzinfo=UTC), 43.0, 35.5),
    ),
    "daily-respiratory-rate": (
        "dailyRespiratoryRate",
        {"date": DATE, "breathsPerMinute": 14.25},
        "fitbit_respiratory_rate_daily",
        "breaths_per_minute",
        (14.25,),
    ),
    "respiratory-rate-sleep-summary": (
        "respiratoryRateSleepSummary",
        {
            "sampleTime": SAMPLE_TIME,
            "fullSleepStats": {
                "breathsPerMinute": 14.0,
                "standardDeviation": 0.5,
                "signalToNoise": 8.0,
            },
            "deepSleepStats": {"breathsPerMinute": 12.0},
            "lightSleepStats": {"breathsPerMinute": 15.0},
            "remSleepStats": {"breathsPerMinute": 16.0},
        },
        "fitbit_respiratory_rate_sleep_summary",
        "full_breaths_per_minute, full_standard_deviation, full_signal_to_noise, "
        "deep_breaths_per_minute, light_breaths_per_minute, rem_breaths_per_minute",
        (14.0, 0.5, 8.0, 12.0, 15.0, 16.0),
    ),
    "daily-oxygen-saturation": (
        "dailyOxygenSaturation",
        {
            "date": DATE,
            "averagePercentage": 97.5,
            "lowerBoundPercentage": 96.0,
            "upperBoundPercentage": 99.0,
            "standardDeviationPercentage": 0.75,
        },
        "fitbit_oxygen_saturation_daily",
        "average_percent, lower_bound_percent, upper_bound_percent, standard_deviation_percent",
        (97.5, 96.0, 99.0, 0.75),
    ),
    "daily-sleep-temperature-derivations": (
        "dailySleepTemperatureDerivations",
        {
            "date": DATE,
            "nightlyTemperatureCelsius": 33.75,
            "baselineTemperatureCelsius": 34.0,
            "relativeNightlyStddev30dCelsius": 0.15,
        },
        "fitbit_sleep_temperature_daily",
        "nightly_temperature_celsius, baseline_temperature_celsius, relative_nightly_stddev_30d_celsius",
        (33.75, 34.0, 0.15),
    ),
    "exercise": (
        "exercise",
        {
            "interval": TIMING,
            "exerciseType": "NEW_EXERCISE",
            "displayName": "Custom walk",
            "activeDuration": "90.5s",
            "metricsSummary": {},
        },
        "fitbit_exercise",
        "exercise_type, display_name, active_duration_seconds",
        ("NEW_EXERCISE", "Custom walk", 90.5),
    ),
    "daily-vo2-max": (
        "dailyVo2Max",
        {
            "date": DATE,
            "vo2Max": 48.5,
            "estimated": False,
            "cardioFitnessLevel": "GOOD",
            "vo2MaxCovariance": 0.25,
        },
        "fitbit_vo2_max_daily",
        "vo2_max_ml_kg_min, estimated, cardio_fitness_level, covariance",
        (48.5, False, "GOOD", 0.25),
    ),
    "sedentary-period": (
        "sedentaryPeriod",
        {"interval": TIMING},
        "fitbit_sedentary_period",
        "start_at, end_at",
        (datetime(2026, 9, 1, 14, 59, tzinfo=UTC), datetime(2026, 9, 1, 15, 1, tzinfo=UTC)),
    ),
    "distance": (
        "distance",
        {"interval": TIMING, "millimeters": "120000"},
        "fitbit_distance_interval",
        "distance_meters",
        (120.0,),
    ),
    "time-in-heart-rate-zone": (
        "timeInHeartRateZone",
        {"interval": TIMING, "heartRateZoneType": "MODERATE"},
        "fitbit_heart_rate_zone_interval",
        "zone",
        ("MODERATE",),
    ),
}


def point(kind, *, payload=None, identity="record-1"):
    field, default, *_ = CASES[kind]
    return {
        "dataPointName": f"users/synthetic/dataTypes/{kind}/dataPoints/{identity}",
        field: deepcopy(default if payload is None else payload),
    }


def snapshot(kind, *, points=None, fetched_at=None, day=DAY):
    from personal_data_platform.sources.fitbit.models import Window

    transport = FakeTransport([{"dataPoints": [point(kind)] if points is None else points}])
    return client(transport, clock=lambda: fetched_at or DAY + timedelta(days=4)).fetch_snapshot(
        Window(kind, day, day + timedelta(days=1)), subject_key="self"
    )
