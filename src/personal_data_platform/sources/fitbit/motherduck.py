"""Replace complete, freshly acquired ranges in the caller's transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .details import SleepSummary
from .models import (
    DAILY_TYPES,
    DATE_TYPES,
    SAMPLE_TYPES,
    TABLES,
    HeartRateMinuteSnapshot,
    Record,
    Snapshot,
    Window,
    WriteResult,
    aware,
)

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def heart_rate_minute_id(data_source_family: str, start: datetime, aggregation_version: str) -> str:
    """Identify a minute independently of account and connection time zone."""
    return _identity(
        [data_source_family, aware(start).isoformat(timespec="microseconds"), aggregation_version]
    )


def sleep_detail_id(kind: str, record_id: str) -> str:
    """Keep provider identities in separate sleep-detail kind namespaces."""
    return _identity([kind, record_id])


def _identity(parts: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


_DAILY_FIELDS = {
    "daily-resting-heart-rate": ("resting_heart_rate_bpm",),
    "daily-heart-rate-variability": (
        "hrv_rmssd_ms",
        "deep_sleep_hrv_rmssd_ms",
        "non_rem_heart_rate_bpm",
        "hrv_entropy",
    ),
    "daily-respiratory-rate": ("respiratory_rate_breaths_per_minute",),
    "daily-oxygen-saturation": (
        "spo2_average_percent",
        "spo2_lower_bound_percent",
        "spo2_upper_bound_percent",
        "spo2_standard_deviation_percent",
    ),
    "daily-sleep-temperature-derivations": (
        "nightly_skin_temperature_celsius",
        "baseline_skin_temperature_celsius",
        "relative_nightly_stddev_30d_celsius",
    ),
    "daily-vo2-max": (
        "vo2_max_ml_kg_min",
        "vo2_max_estimated",
        "cardio_fitness_level",
        "vo2_max_covariance",
    ),
}
_ALL_DAILY_FIELDS = tuple(field for fields in _DAILY_FIELDS.values() for field in fields)


def record_values(record: Record) -> dict[str, object]:
    """Map every typed provider field to its public base column."""
    kind = record.kind
    values: dict[str, object] = {"source_date": record.source_date}
    details = asdict(record.details) if record.details is not None else {}
    if kind in DAILY_TYPES:
        if kind in ("daily-resting-heart-rate", "daily-respiratory-rate"):
            metrics: tuple[object, ...] = (record.value,)
        else:
            metrics = tuple(details.values())
        return {**values, **dict(zip(_DAILY_FIELDS[kind], metrics, strict=True))}
    if kind in SAMPLE_TYPES:
        values.update(sample_at=record.start, offset_seconds=record.offset_seconds)
        if kind == "respiratory-rate-sleep-summary":
            values.update(
                {
                    f"{stage}_{field}": value
                    for stage, stats in details.items()
                    for field, value in stats.items()
                }
            )
        else:
            values.update(details)
        return values
    values.update(
        start_at=record.start,
        end_at=record.end,
        offset_seconds=record.offset_seconds,
        end_offset_seconds=record.end_offset_seconds,
    )
    if kind == "sleep":
        summary = record.details if record.details is not None else SleepSummary()
        assert isinstance(summary, SleepSummary)
        values.update(
            sleep_minutes=record.value,
            sleep_type=record.category,
            is_main_sleep=record.is_main_sleep,
            awake_minutes=summary.minutes_awake,
            sleep_period_minutes=summary.minutes_in_sleep_period,
            sleep_latency_minutes=summary.minutes_to_fall_asleep,
            after_wake_minutes=summary.minutes_after_wake_up,
        )
    elif kind in ("sleep-stage", "sleep-wake"):
        values.pop("source_date")
        values.update(sleep_session_id=record.parent_id, kind=kind, category=record.category)
    elif kind == "exercise":
        values.update(
            exercise_type=details["exercise_type"],
            exercise_name=details["display_name"],
            active_seconds=details["active_duration_seconds"],
        )
    elif kind == "steps":
        values["steps"] = record.value
    elif kind == "active-zone-minutes":
        values.update(active_zone_minutes=record.value, category=record.category)
    elif kind == "distance":
        values["distance_meters"] = record.value
    elif kind == "time-in-heart-rate-zone":
        values["zone"] = record.category
    return values


def _column_type(column: str) -> str:
    if column == "source_date":
        return "DATE"
    if column.endswith("_at"):
        return "TIMESTAMPTZ"
    if column in ("offset_seconds", "end_offset_seconds"):
        return "INTEGER"
    if column == "non_rem_heart_rate_bpm":
        return "BIGINT"
    if column in ("is_main_sleep", "vo2_max_estimated"):
        return "BOOLEAN"
    if column.endswith("_id") or column in (
        "kind",
        "category",
        "sleep_type",
        "exercise_type",
        "exercise_name",
        "zone",
        "cardio_fitness_level",
        "aggregation_version",
    ):
        return "VARCHAR"
    return "DOUBLE"


def _upsert(
    connection: DuckDBPyConnection, table: str, key: str, rows: list[dict[str, object]]
) -> None:
    if not rows:
        return
    columns = tuple(rows[0])
    projection = ",".join(f"r.{field}::{_column_type(field)}" for field in columns)
    updates = ",".join(f"{field}=excluded.{field}" for field in columns if field != key)
    connection.execute(
        f"INSERT INTO base.{table} ({','.join(columns)}) "
        f"SELECT {projection} FROM unnest(?) incoming(r) "
        f"ON CONFLICT ({key}) DO UPDATE SET {updates}",
        [rows],
    )


def _delete(connection: DuckDBPyConnection, table: str, key: str, ids: list[object]) -> None:
    if ids:
        connection.execute(
            f"DELETE FROM base.{table} WHERE {key} IN (SELECT unnest(?::{_column_type(key)}[]))",
            [ids],
        )


def _reconcile(
    connection: DuckDBPyConnection,
    table: str,
    key: str,
    incoming: list[dict[str, object]],
    *,
    predicate: str,
    parameters: list[object],
) -> tuple[int, bool]:
    """Compare current values, including incoming identities moved from outside the range."""
    columns = tuple(incoming[0]) if incoming else ()
    ids = [row[key] for row in incoming]
    existing = connection.execute(
        f"SELECT * FROM base.{table} WHERE ({predicate}) "
        f"OR {key} IN (SELECT unnest(?::{_column_type(key)}[]))",
        [*parameters, ids],
    )
    names = [description[0] for description in existing.description]
    current = {
        row[names.index(key)]: dict(zip(names, row, strict=True)) for row in existing.fetchall()
    }
    omitted = [identity for identity in current if identity not in ids]
    changed = [
        row
        for row in incoming
        if row[key] not in current
        or any(current[row[key]][field] != row[field] for field in columns)
    ]
    _delete(connection, table, key, omitted)
    _upsert(connection, table, key, changed)
    return len(changed), bool(omitted or changed)


def _range(window: Window) -> tuple[str, list[object]]:
    if window.data_type in DATE_TYPES or window.time_basis == "civil":
        return "source_date >= ? AND source_date < ?", [window.start.date(), window.end.date()]
    cursor = "sample_at" if window.data_type in SAMPLE_TYPES else "start_at"
    return f"{cursor} >= ? AND {cursor} < ?", [window.start, window.end]


def _daily(connection: DuckDBPyConnection, snapshot: Snapshot) -> tuple[int, bool]:
    fields = _DAILY_FIELDS[snapshot.window.data_type]
    current = dict(
        connection.execute(
            f"SELECT source_date,struct_pack({','.join(f'{field}:={field}' for field in _ALL_DAILY_FIELDS)}) "
            "FROM base.health_daily WHERE source_date>=? AND source_date<?",
            [snapshot.window.start.date(), snapshot.window.end.date()],
        ).fetchall()
    )
    incoming = {record.source_date: record_values(record) for record in snapshot.records}
    changed: list[dict[str, object]] = []
    deleted: list[object] = []
    submitted = 0
    for day in current.keys() | incoming.keys():
        row = incoming.get(day, {"source_date": day, **dict.fromkeys(fields)})
        previous = current.get(day, dict.fromkeys(_ALL_DAILY_FIELDS))
        if all(previous[field] == row[field] for field in fields):
            continue
        merged = {**previous, **row}
        if all(merged[field] is None for field in _ALL_DAILY_FIELDS):
            deleted.append(day)
        else:
            changed.append(row)
            submitted += int(day in incoming)
    _delete(connection, "health_daily", "source_date", deleted)
    _upsert(connection, "health_daily", "source_date", changed)
    return submitted, bool(changed or deleted)


@dataclass(frozen=True, slots=True)
class FitbitBatch:
    snapshot: Snapshot

    def write_snapshot(self, connection: DuckDBPyConnection) -> WriteResult:
        snapshot = self.snapshot
        kind = snapshot.window.data_type
        predicate, parameters = _range(snapshot.window)
        if kind in DAILY_TYPES:
            submitted, changed = _daily(connection, snapshot)
        else:
            table = TABLES[kind]
            key = (
                f"{table}_id"
                if kind != "respiratory-rate-sleep-summary"
                else "respiratory_summary_id"
            )
            main = [record for record in snapshot.records if record.kind == kind]
            incoming = [{key: record.record_id, **record_values(record)} for record in main]
            submitted = 0
            changed = False
            if kind == "sleep":
                parent_ids = [record.record_id for record in main]
                affected = [
                    row[0]
                    for row in connection.execute(
                        f"SELECT sleep_session_id FROM base.sleep_session WHERE {predicate}",
                        parameters,
                    ).fetchall()
                ]
                child_rows = [
                    {
                        "sleep_detail_id": sleep_detail_id(record.kind, record.record_id),
                        **record_values(record),
                    }
                    for record in snapshot.records
                    if record.kind != "sleep"
                ]
                count, child_changed = _reconcile(
                    connection,
                    "sleep_detail",
                    "sleep_detail_id",
                    child_rows,
                    predicate="sleep_session_id IN (SELECT unnest(?::VARCHAR[]))",
                    parameters=[list(set(parent_ids + affected))],
                )
                submitted += count
                changed |= child_changed
            count, main_changed = _reconcile(
                connection, table, key, incoming, predicate=predicate, parameters=parameters
            )
            submitted += count
            changed |= main_changed
        return WriteResult(
            "range_replaced" if changed else "unchanged",
            submitted,
            ((snapshot.window.start, snapshot.window.end),),
        )


@dataclass(frozen=True, slots=True)
class FitbitMinuteBatch:
    snapshot: HeartRateMinuteSnapshot

    def write_snapshot(self, connection: DuckDBPyConnection) -> WriteResult:
        snapshot = self.snapshot
        rows = [
            {
                "heart_rate_minute_id": heart_rate_minute_id(
                    minute.data_source_family, minute.start, minute.aggregation_version
                ),
                "start_at": minute.start,
                "mean_bpm": minute.average,
                "min_bpm": minute.minimum,
                "max_bpm": minute.maximum,
                "aggregation_version": minute.aggregation_version,
            }
            for minute in snapshot.minutes
        ]
        submitted, changed = _reconcile(
            connection,
            "heart_rate_minute",
            "heart_rate_minute_id",
            rows,
            predicate="start_at>=? AND start_at<?",
            parameters=[snapshot.window.start, snapshot.window.end],
        )
        return WriteResult(
            "range_replaced" if changed else "unchanged",
            submitted,
            ((snapshot.window.start, snapshot.window.end),),
        )
