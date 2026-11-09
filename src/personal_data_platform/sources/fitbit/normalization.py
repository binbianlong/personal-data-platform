"""Normalize Google Health source points into validated records."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, date, datetime, timedelta

from .details import (
    ExerciseDetails,
    HrvDaily,
    HrvSample,
    OxygenSaturationDaily,
    RecordDetails,
    RespiratorySleepSummary,
    RespiratoryStats,
    SleepSummary,
    SleepTemperatureDaily,
    Vo2MaxDaily,
)
from .models import (
    DAILY_TYPES,
    GOOGLE_WEARABLES,
    SAMPLE_TYPES,
    HeartRateMinute,
    Record,
    date_cursor,
    object_dict,
    parse_time,
    string,
)

_FIELDS = {
    "steps": "steps",
    "daily-resting-heart-rate": "dailyRestingHeartRate",
    "active-zone-minutes": "activeZoneMinutes",
    "sleep": "sleep",
    "daily-heart-rate-variability": "dailyHeartRateVariability",
    "heart-rate-variability": "heartRateVariability",
    "daily-respiratory-rate": "dailyRespiratoryRate",
    "respiratory-rate-sleep-summary": "respiratoryRateSleepSummary",
    "daily-oxygen-saturation": "dailyOxygenSaturation",
    "daily-sleep-temperature-derivations": "dailySleepTemperatureDerivations",
    "daily-vo2-max": "dailyVo2Max",
    "exercise": "exercise",
    "sedentary-period": "sedentaryPeriod",
    "distance": "distance",
    "time-in-heart-rate-zone": "timeInHeartRateZone",
}
_POINT_FIELDS = set(_FIELDS.values()) | {"heartRate"}


_ROLLUP_OTHER_FIELDS = (
    "steps",
    "floors",
    "weight",
    "altitude",
    "distance",
    "bodyFat",
    "totalCalories",
    "activeZoneMinutes",
    "sedentaryPeriod",
    "runVo2Max",
    "caloriesInHeartRateZone",
    "activityLevel",
    "nutritionLog",
    "hydrationLog",
    "timeInHeartRateZone",
    "activeMinutes",
    "swimLengthsData",
    "coreBodyTemperature",
    "activeEnergyBurned",
    "bloodGlucose",
)


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _rollup_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("heart rate statistic must be numeric")
    return float(value)


def _integer(value: object, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("expected an integer")
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value) is None:
        raise ValueError("expected an integer")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError("integer outside permitted range")
    return result


def parse_timestamp(value: object) -> datetime:
    text = string(value)
    fraction = re.search(r"\.(\d+)", text)
    if fraction is not None and any(digit != "0" for digit in fraction[1][6:]):
        raise ValueError("timestamp precision exceeds storage precision")
    return parse_time(text)


def _offset(value: object) -> int:
    text = string(value)
    if re.fullmatch(r"-?\d+(?:\.0+)?s", text) is None:
        raise ValueError("UTC offset must use whole seconds")
    return _integer(text[:-1].split(".")[0], minimum=-64800, maximum=64800)


def _date(value: object) -> date:
    fields = object_dict(value)
    return date(
        _integer(fields["year"], minimum=1, maximum=9999),
        _integer(fields["month"], minimum=1, maximum=12),
        _integer(fields["day"], minimum=1, maximum=31),
    )


def _source_date(value: object, instant: datetime, offset: int) -> date:
    local_date = (instant + timedelta(seconds=offset)).date()
    if value is None:
        return local_date
    provided = _date(object_dict(value)["date"])
    if provided != local_date:
        raise ValueError("provider civil date disagrees with time and UTC offset")
    return provided


def _identity(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _alias(fields: dict[str, object], primary: str, alternate: str) -> object:
    # The REST schema and guide examples use different names. Accept either,
    # but never silently choose between contradictory provider identities.
    if (
        primary in fields
        and alternate in fields
        and (
            type(fields[primary]) is not type(fields[alternate])
            or fields[primary] != fields[alternate]
        )
    ):
        raise ValueError("conflicting API field aliases")
    return fields[primary] if primary in fields else fields.get(alternate)


def _point_id(point: dict[str, object], data_type: str) -> str | None:
    name = _alias(point, "dataPointName", "name")
    if name in (None, ""):
        return None
    # Respiratory summaries can return the collection stem without a point ID.
    if data_type == "respiratory-rate-sleep-summary" and re.fullmatch(
        r"users/[^/]+/dataTypes/respiratory-rate-sleep-summary/dataPoints/", string(name)
    ):
        return None
    matched = re.fullmatch(
        rf"users/[^/]+/dataTypes/{re.escape(data_type)}/dataPoints/([^/]+)", string(name)
    )
    if matched is None:
        raise ValueError("invalid data point identity")
    return matched[1]


def _interval(fields: dict[str, object]) -> tuple[datetime, datetime, int, int]:
    start, end = parse_timestamp(fields["startTime"]), parse_timestamp(fields["endTime"])
    if start >= end:
        raise ValueError("interval must be positive")
    return start, end, _offset(fields["startUtcOffset"]), _offset(fields["endUtcOffset"])


def normalize(data_type: str, point: dict[str, object], subject_key: str) -> tuple[Record, ...]:
    field = _FIELDS[data_type]
    if any(other != field and other in point for other in _POINT_FIELDS):
        raise ValueError("data point contains multiple union fields")
    payload = object_dict(point[field])
    point_id = _point_id(point, data_type)
    if data_type == "sleep":
        if point_id is None:
            raise ValueError("sleep needs a provider identity")
        return _sleep(payload, point_id)
    if data_type == "daily-resting-heart-rate":
        source_date = _date(payload["date"])
        cursor = date_cursor(source_date)
        return (
            Record(
                kind=data_type,
                record_id=point_id or _identity(subject_key, data_type, source_date.isoformat()),
                cursor=cursor,
                start=cursor,
                source_date=source_date,
                value=float(_integer(payload["beatsPerMinute"], minimum=1, maximum=300)),
            ),
        )
    if data_type not in ("steps", "active-zone-minutes"):
        return (_health_metric(data_type, payload, point_id, subject_key),)
    timing = object_dict(payload["interval"])
    start, end, start_offset, end_offset = _interval(timing)
    # Steps records omit count for an on-wrist true zero; no record means missing data.
    raw_value = payload.get("count", 0) if data_type == "steps" else payload["activeZoneMinutes"]
    value = _integer(raw_value, maximum=1_000_000 if data_type == "steps" else 2**63 - 1)
    category = None
    if data_type == "active-zone-minutes":
        category = string(payload["heartRateZone"]).lower()
        if category not in ("fat_burn", "cardio", "peak", "heart_rate_zone_unspecified"):
            raise ValueError("unrecognized heart rate zone")
    return (
        Record(
            kind=data_type,
            record_id=point_id or _identity(subject_key, data_type, utc_text(start), utc_text(end)),
            cursor=start,
            start=start,
            end=end,
            value=float(value),
            offset_seconds=start_offset,
            end_offset_seconds=end_offset,
            source_date=_source_date(timing.get("civilStartTime"), start, start_offset),
            category=category,
        ),
    )


def _sleep(payload: dict[str, object], point_id: str) -> tuple[Record, ...]:
    timing = object_dict(payload["interval"])
    start, end, start_offset, end_offset = _interval(timing)
    source_date = _source_date(timing.get("civilEndTime"), end, end_offset)
    cursor = date_cursor(source_date)
    summary = object_dict(payload.get("summary", {}))
    minutes = summary.get("minutesAsleep")
    main_sleep = _alias(object_dict(payload.get("metadata", {})), "mainSleep", "main")
    if main_sleep is not None and not isinstance(main_sleep, bool):
        raise ValueError("sleep classification must be boolean")
    category = string(payload["type"]).lower() if "type" in payload else None
    records = [
        Record(
            kind="sleep",
            record_id=point_id,
            cursor=cursor,
            start=start,
            end=end,
            offset_seconds=start_offset,
            end_offset_seconds=end_offset,
            source_date=source_date,
            value=float(_integer(minutes)) if minutes is not None else None,
            is_main_sleep=main_sleep,
            category=category,
            details=SleepSummary(
                _optional_integer(summary, "minutesAwake"),
                _optional_integer(summary, "minutesInSleepPeriod"),
                _optional_integer(summary, "minutesToFallAsleep"),
                _optional_integer(summary, "minutesAfterWakeUp"),
            ),
        )
    ]
    stage_intervals: list[tuple[datetime, datetime]] = []
    for field, kind, wake_category in (
        ("stages", "sleep-stage", None),
        ("shortAwakenings", "sleep-wake", "short-awakening"),
        ("outOfBedSegments", "sleep-wake", "out-of-bed"),
    ):
        children = payload.get(field, [])
        if not isinstance(children, list):
            raise ValueError("sleep details must be an array")
        for child in children:
            detail = object_dict(child)
            child_start, child_end = (
                parse_timestamp(detail["startTime"]),
                parse_timestamp(detail["endTime"]),
            )
            if not start <= child_start < child_end <= end:
                raise ValueError("sleep detail outside session")
            detail_category = wake_category or string(detail["type"]).lower()
            if kind == "sleep-stage":
                if detail_category not in (
                    "awake",
                    "light",
                    "deep",
                    "rem",
                    "asleep",
                    "restless",
                    "sleep_stage_type_unspecified",
                ):
                    raise ValueError("unrecognized sleep stage")
                stage_intervals.append((child_start, child_end))
            # The guide's shortAwakenings objects omit local offsets. Keep them
            # unknown rather than inferring a timezone across an offset change.
            child_start_offset = (
                _offset(detail["startUtcOffset"]) if "startUtcOffset" in detail else None
            )
            child_end_offset = _offset(detail["endUtcOffset"]) if "endUtcOffset" in detail else None
            if field != "shortAwakenings" and (
                child_start_offset is None or child_end_offset is None
            ):
                raise ValueError("sleep detail needs its UTC offsets")
            records.append(
                Record(
                    kind=kind,
                    record_id=_identity(
                        point_id,
                        kind,
                        wake_category or "",
                        utc_text(child_start),
                        utc_text(child_end),
                    ),
                    cursor=cursor,
                    start=child_start,
                    end=child_end,
                    offset_seconds=child_start_offset,
                    end_offset_seconds=child_end_offset,
                    source_date=source_date,
                    parent_id=point_id,
                    category=detail_category,
                    value=(child_end - child_start).total_seconds(),
                )
            )
    ordered_stages = sorted(stage_intervals)
    for previous, following in zip(ordered_stages, ordered_stages[1:]):
        if previous[1] > following[0]:
            raise ValueError("sleep stages must not overlap")
    return tuple(records)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("metric must be a finite number")
    return float(value)


def _optional_number(payload: dict[str, object], field: str) -> float | None:
    value = payload.get(field)
    return None if value is None else _number(value)


def _optional_integer(payload: dict[str, object], field: str) -> int | None:
    value = payload.get(field)
    return None if value is None else _integer(value)


def _duration(value: object) -> float:
    text = string(value)
    if re.fullmatch(r"\d+(?:\.\d{1,9})?s", text) is None:
        raise ValueError("duration must be nonnegative seconds")
    seconds = float(text[:-1])
    if not math.isfinite(seconds):
        raise ValueError("duration must be finite")
    return seconds


def _respiratory_stats(value: object) -> RespiratoryStats:
    fields = object_dict(value)
    return RespiratoryStats(
        _number(fields["breathsPerMinute"]),
        _optional_number(fields, "standardDeviation"),
        _optional_number(fields, "signalToNoise"),
    )


def _health_metric(
    kind: str, payload: dict[str, object], point_id: str | None, subject_key: str
) -> Record:
    details: RecordDetails | None = None
    value = None
    if kind in DAILY_TYPES:
        source_date = _date(payload["date"])
        cursor = date_cursor(source_date)
        if kind == "daily-heart-rate-variability":
            details = HrvDaily(
                _optional_number(payload, "averageHeartRateVariabilityMilliseconds"),
                _optional_number(
                    payload, "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds"
                ),
                _optional_integer(payload, "nonRemHeartRateBeatsPerMinute"),
                _optional_number(payload, "entropy"),
            )
        elif kind == "daily-respiratory-rate":
            value = _number(payload["breathsPerMinute"])
        elif kind == "daily-oxygen-saturation":
            details = OxygenSaturationDaily(
                _number(payload["averagePercentage"]),
                _number(payload["lowerBoundPercentage"]),
                _number(payload["upperBoundPercentage"]),
                _optional_number(payload, "standardDeviationPercentage"),
            )
        elif kind == "daily-sleep-temperature-derivations":
            details = SleepTemperatureDaily(
                _number(payload["nightlyTemperatureCelsius"]),
                _optional_number(payload, "baselineTemperatureCelsius"),
                _optional_number(payload, "relativeNightlyStddev30dCelsius"),
            )
        elif kind == "daily-vo2-max":
            estimated = payload.get("estimated")
            if estimated is not None and type(estimated) is not bool:
                raise ValueError("estimated must be boolean")
            level = payload.get("cardioFitnessLevel")
            details = Vo2MaxDaily(
                _number(payload["vo2Max"]),
                estimated,
                string(level) if level is not None else None,
                _optional_number(payload, "vo2MaxCovariance"),
            )
        return Record(
            kind,
            point_id or _identity(subject_key, kind, source_date.isoformat()),
            cursor,
            cursor,
            source_date=source_date,
            value=value,
            details=details,
        )
    if kind in SAMPLE_TYPES:
        timing = object_dict(payload["sampleTime"])
        at = parse_timestamp(timing["physicalTime"])
        offset = _offset(timing["utcOffset"])
        source_date = _source_date(timing.get("civilTime"), at, offset)
        if kind == "heart-rate-variability":
            details = HrvSample(
                _optional_number(payload, "rootMeanSquareOfSuccessiveDifferencesMilliseconds"),
                _optional_number(payload, "standardDeviationMilliseconds"),
            )
        else:
            details = RespiratorySleepSummary(
                _respiratory_stats(payload["fullSleepStats"]),
                *(
                    _respiratory_stats(payload[field]) if field in payload else RespiratoryStats()
                    for field in ("deepSleepStats", "lightSleepStats", "remSleepStats")
                ),
            )
        return Record(
            kind,
            point_id or _identity(subject_key, kind, utc_text(at)),
            at,
            at,
            offset_seconds=offset,
            source_date=source_date,
            details=details,
        )
    timing = object_dict(payload["interval"])
    start, end, start_offset, end_offset = _interval(timing)
    source_date = _source_date(timing.get("civilStartTime"), start, start_offset)
    cursor = start
    category = None
    if kind == "exercise":
        if point_id is None:
            raise ValueError("exercise needs a provider identity")
        cursor = date_cursor(source_date)
        duration = payload.get("activeDuration")
        details = ExerciseDetails(
            string(payload["exerciseType"]),
            string(payload["displayName"]),
            _duration(duration) if duration is not None else None,
        )
    elif kind == "distance":
        # Like steps, the provider can omit the value for an observed true zero.
        value = _integer(payload.get("millimeters", 0), maximum=1_000_000_000) / 1000
    elif kind == "time-in-heart-rate-zone":
        category = string(payload["heartRateZoneType"])
    return Record(
        kind,
        point_id or _identity(subject_key, kind, utc_text(start), utc_text(end), category or ""),
        cursor,
        start,
        end,
        value,
        start_offset,
        end_offset,
        source_date,
        category=category,
        details=details,
    )


def normalize_heart_rate_minute(
    point: dict[str, object], *, start: datetime, end: datetime
) -> HeartRateMinute | None:
    if any(field in point for field in _ROLLUP_OTHER_FIELDS):
        raise ValueError("rollup has a conflicting data type")
    if "heartRate" not in point:
        return None
    stats = object_dict(point["heartRate"])
    return HeartRateMinute(
        start,
        end,
        _rollup_number(stats["beatsPerMinuteAvg"]),
        _rollup_number(stats["beatsPerMinuteMin"]),
        _rollup_number(stats["beatsPerMinuteMax"]),
        GOOGLE_WEARABLES,
    )
