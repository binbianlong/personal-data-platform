"""Typed values carried by multi-value Google Health records."""

from __future__ import annotations

import math
from dataclasses import dataclass


def _numbers(*values: float | None, signed: bool = False) -> None:
    for value in values:
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or (not signed and value < 0)
        ):
            raise ValueError("metric must be finite and within its permitted range")


@dataclass(frozen=True, slots=True)
class HrvDaily:
    rmssd_ms: float | None = None
    deep_sleep_rmssd_ms: float | None = None
    non_rem_heart_rate_bpm: int | None = None
    entropy: float | None = None

    def __post_init__(self) -> None:
        values = (
            self.rmssd_ms,
            self.deep_sleep_rmssd_ms,
            self.non_rem_heart_rate_bpm,
            self.entropy,
        )
        _numbers(*values)
        if all(value is None for value in values):
            raise ValueError("daily HRV needs at least one measurement")


@dataclass(frozen=True, slots=True)
class HrvSample:
    rmssd_ms: float | None = None
    sdnn_ms: float | None = None

    def __post_init__(self) -> None:
        _numbers(self.rmssd_ms, self.sdnn_ms)


@dataclass(frozen=True, slots=True)
class RespiratoryStats:
    breaths_per_minute: float | None = None
    standard_deviation: float | None = None
    signal_to_noise: float | None = None

    def __post_init__(self) -> None:
        _numbers(self.breaths_per_minute, self.standard_deviation)
        _numbers(self.signal_to_noise, signed=True)


@dataclass(frozen=True, slots=True)
class RespiratorySleepSummary:
    full: RespiratoryStats = RespiratoryStats()
    deep: RespiratoryStats = RespiratoryStats()
    light: RespiratoryStats = RespiratoryStats()
    rem: RespiratoryStats = RespiratoryStats()


@dataclass(frozen=True, slots=True)
class OxygenSaturationDaily:
    average_percent: float
    lower_bound_percent: float
    upper_bound_percent: float
    standard_deviation_percent: float | None = None

    def __post_init__(self) -> None:
        values = (self.average_percent, self.lower_bound_percent, self.upper_bound_percent)
        _numbers(*values, self.standard_deviation_percent)
        if any(value > 100 for value in values):
            raise ValueError("oxygen saturation must be a percentage")


@dataclass(frozen=True, slots=True)
class SleepTemperatureDaily:
    nightly_temperature_celsius: float
    baseline_temperature_celsius: float | None = None
    relative_nightly_stddev_30d_celsius: float | None = None

    def __post_init__(self) -> None:
        _numbers(self.nightly_temperature_celsius, self.baseline_temperature_celsius, signed=True)
        _numbers(self.relative_nightly_stddev_30d_celsius)


@dataclass(frozen=True, slots=True)
class Vo2MaxDaily:
    vo2_max_ml_kg_min: float
    estimated: bool | None = None
    cardio_fitness_level: str | None = None
    covariance: float | None = None

    def __post_init__(self) -> None:
        _numbers(self.vo2_max_ml_kg_min, self.covariance)
        if self.vo2_max_ml_kg_min > 100:
            raise ValueError("VO2 max outside permitted range")
        if self.estimated is not None and type(self.estimated) is not bool:
            raise ValueError("estimated must be boolean")
        if self.cardio_fitness_level is not None and not isinstance(self.cardio_fitness_level, str):
            raise ValueError("fitness level must be a string")


@dataclass(frozen=True, slots=True)
class ExerciseDetails:
    exercise_type: str
    display_name: str
    active_duration_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.exercise_type, str) or not self.exercise_type:
            raise ValueError("exercise needs a type")
        if not isinstance(self.display_name, str):
            raise ValueError("exercise name must be a string")
        _numbers(self.active_duration_seconds)


@dataclass(frozen=True, slots=True)
class SleepSummary:
    minutes_awake: int | None = None
    minutes_in_sleep_period: int | None = None
    minutes_to_fall_asleep: int | None = None
    minutes_after_wake_up: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.minutes_awake,
            self.minutes_in_sleep_period,
            self.minutes_to_fall_asleep,
            self.minutes_after_wake_up,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("sleep summary minutes must be nonnegative integers")


RecordDetails = (
    HrvDaily
    | HrvSample
    | RespiratorySleepSummary
    | OxygenSaturationDaily
    | SleepTemperatureDaily
    | Vo2MaxDaily
    | ExerciseDetails
    | SleepSummary
)
DETAIL_TYPES = {
    "daily-heart-rate-variability": HrvDaily,
    "heart-rate-variability": HrvSample,
    "respiratory-rate-sleep-summary": RespiratorySleepSummary,
    "daily-oxygen-saturation": OxygenSaturationDaily,
    "daily-sleep-temperature-derivations": SleepTemperatureDaily,
    "daily-vo2-max": Vo2MaxDaily,
    "exercise": ExerciseDetails,
    "sleep": SleepSummary,
}
