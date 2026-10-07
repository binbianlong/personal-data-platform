"""Replace complete acquired ranges inside the warehouse's transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .models import (
    GOOGLE_WEARABLES,
    RECORD_AGGREGATION_VERSION,
    TABLES,
    HeartRateMinuteSnapshot,
    Record,
    Snapshot,
    Window,
)

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

_SCOPE = "subject_key=? AND data_type=? AND data_source_family=? AND aggregation_version=?"


def coverage_scope(
    subject_key: str, data_type: str, aggregation_version: str = RECORD_AGGREGATION_VERSION
) -> tuple[str, str, str, str]:
    return subject_key, data_type, GOOGLE_WEARABLES, aggregation_version


def _subtract(
    ranges: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    result = []
    for left, right in ranges:
        if end <= left or start >= right:
            result.append((left, right))
        else:
            if left < start:
                result.append((left, start))
            if end < right:
                result.append((end, right))
    return result


@dataclass(frozen=True, slots=True)
class _Coverage:
    start: datetime
    end: datetime
    fetched_at: datetime
    source_key: str
    content_sha256: str
    source_sha256: str


def _read_coverage(
    connection: DuckDBPyConnection, scope: tuple[str, str, str, str], window: Window
) -> list[_Coverage]:
    rows = connection.execute(
        "SELECT range_start,range_end,fetched_at,source_key,content_sha256,source_sha256 "
        f"FROM ops.fitbit_coverage WHERE {_SCOPE} "
        "AND range_start < ? AND range_end > ? ORDER BY range_start",
        [*scope, window.end, window.start],
    ).fetchall()
    return [_Coverage(*row) for row in rows]


def _accepted_ranges(
    snapshot: Snapshot | HeartRateMinuteSnapshot, source_key: str, coverage: list[_Coverage]
) -> list[tuple[datetime, datetime]]:
    accepted = [(snapshot.window.start, snapshot.window.end)]
    for prior in coverage:
        if (prior.fetched_at, prior.source_key) > (snapshot.fetched_at, source_key):
            accepted = _subtract(accepted, prior.start, prior.end)
    return accepted


def _unchanged(coverage: list[_Coverage], window: Window, digest: str) -> bool:
    return (
        len(coverage) == 1
        and coverage[0].start == window.start
        and coverage[0].end == window.end
        and coverage[0].content_sha256 == digest
    )


def _replace_coverage(
    connection: DuckDBPyConnection,
    scope: tuple[str, str, str, str],
    coverage: list[_Coverage],
    accepted: list[tuple[datetime, datetime]],
    *,
    fetched_at: datetime,
    source_key: str,
    digest: str,
    source_digest: str,
) -> None:
    # Split empty and nonempty acquisitions alike to protect newer subranges.
    for prior in coverage:
        remaining = [(prior.start, prior.end)]
        for start, end in accepted:
            remaining = _subtract(remaining, start, end)
        if remaining == [(prior.start, prior.end)]:
            continue
        connection.execute(
            f"DELETE FROM ops.fitbit_coverage WHERE {_SCOPE} AND range_start=?",
            [*scope, prior.start],
        )
        for start, end in remaining:
            # A fragment is not a complete acquisition; neither hash is reusable.
            connection.execute(
                "INSERT INTO ops.fitbit_coverage VALUES (?,?,?,?,?,?,?,?,?,?)",
                [*scope, start, end, prior.fetched_at, prior.source_key, "", ""],
            )
    for start, end in accepted:
        connection.execute(
            "INSERT INTO ops.fitbit_coverage VALUES (?,?,?,?,?,?,?,?,?,?)",
            [*scope, start, end, fetched_at, source_key, digest, source_digest],
        )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _cursor(kind: str, prefix: str = "") -> str:
    if kind in ("steps", "active-zone-minutes"):
        return f"{prefix}start_at"
    # DATE::TIMESTAMPTZ would depend on the connection's time zone.
    return f"timezone('UTC', {prefix}source_date::TIMESTAMP)"


def _kind_filter(kind: str, prefix: str = "") -> str:
    if kind in ("steps", "active-zone-minutes"):
        return f"{prefix}metric='{kind}'"
    if kind in ("sleep-stage", "sleep-wake"):
        return f"{prefix}kind='{kind}'"
    return "true"


def _record_fields(kind: str) -> tuple[tuple[str, ...], str]:
    if kind in ("steps", "active-zone-minutes"):
        return (
            (
                "metric",
                "start_at",
                "end_at",
                "value",
                "offset_seconds",
                "end_offset_seconds",
                "source_date",
                "category",
            ),
            "r.kind::VARCHAR, r.start::TIMESTAMPTZ, r.end::TIMESTAMPTZ, r.value::DOUBLE, "
            "r.offset_seconds::INTEGER, r.end_offset_seconds::INTEGER, r.source_date::DATE, r.category::VARCHAR",
        )
    if kind == "daily-resting-heart-rate":
        return ("source_date", "beats_per_minute"), "r.source_date::DATE, r.value::DOUBLE"
    if kind == "sleep":
        return (
            (
                "source_date",
                "start_at",
                "end_at",
                "sleep_minutes",
                "offset_seconds",
                "end_offset_seconds",
                "sleep_type",
                "is_main_sleep",
            ),
            "r.source_date::DATE, r.start::TIMESTAMPTZ, r.end::TIMESTAMPTZ, r.value::DOUBLE, "
            "r.offset_seconds::INTEGER, r.end_offset_seconds::INTEGER, r.category::VARCHAR, r.is_main_sleep::BOOLEAN",
        )
    return (
        (
            "kind",
            "sleep_id",
            "source_date",
            "start_at",
            "end_at",
            "offset_seconds",
            "end_offset_seconds",
            "category",
        ),
        "r.kind::VARCHAR, r.parent_id::VARCHAR, (r.cursor::TIMESTAMPTZ AT TIME ZONE 'UTC')::DATE, "
        "r.start::TIMESTAMPTZ, r.end::TIMESTAMPTZ, r.offset_seconds::INTEGER, "
        "r.end_offset_seconds::INTEGER, r.category::VARCHAR",
    )


@dataclass(frozen=True, slots=True)
class FitbitBatch:
    snapshot: Snapshot
    source_digest: str | None = None

    def write_snapshot(
        self, connection: DuckDBPyConnection, *, source_key: str, loaded_at: datetime
    ) -> None:
        snapshot = self.snapshot
        window = snapshot.window
        scope = coverage_scope(snapshot.subject_key, window.data_type)
        coverage = _read_coverage(connection, scope, window)
        accepted = _accepted_ranges(snapshot, source_key, coverage)
        if not accepted:
            return
        records = [asdict(record) for record in snapshot.records]
        records.sort(key=lambda item: json.dumps(item, default=str, sort_keys=True))
        digest = _digest(records)
        source_digest = (
            self.source_digest if self.source_digest is not None else snapshot.source_sha256()
        )
        if not _unchanged(coverage, window, digest):
            protected = self._replace_records(
                connection, accepted, source_key=source_key, loaded_at=loaded_at
            )
            if protected:
                digest = source_digest = ""
        if accepted != [(window.start, window.end)]:
            source_digest = ""
        _replace_coverage(
            connection,
            scope,
            coverage,
            accepted,
            fetched_at=snapshot.fetched_at,
            source_key=source_key,
            digest=digest,
            source_digest=source_digest,
        )

    def _replace_records(
        self,
        connection: DuckDBPyConnection,
        accepted: list[tuple[datetime, datetime]],
        *,
        source_key: str,
        loaded_at: datetime,
    ) -> set[str]:
        snapshot = self.snapshot
        kind = snapshot.window.data_type
        main = [
            record
            for record in snapshot.records
            if record.kind == kind and any(start <= record.cursor < end for start, end in accepted)
        ]
        protected = (
            self._protect_existing_records(connection, main, accepted, source_key=source_key)
            if main
            else set()
        )
        ids = [record.record_id for record in main if record.record_id not in protected]
        if kind == "sleep" and ids:
            connection.execute(
                "DELETE FROM base.fitbit_sleep_detail WHERE subject_key=? "
                "AND sleep_id IN (SELECT unnest(?::VARCHAR[]))",
                [snapshot.subject_key, ids],
            )
        self._update_deletions(
            connection, accepted, ids, protected=protected, source_key=source_key
        )
        self._replace_rows(
            connection, accepted, protected, source_key=source_key, loaded_at=loaded_at
        )
        return protected

    def _protect_existing_records(
        self,
        connection: DuckDBPyConnection,
        records: list[Record],
        accepted: list[tuple[datetime, datetime]],
        *,
        source_key: str,
    ) -> set[str]:
        snapshot = self.snapshot
        kind = snapshot.window.data_type
        scope = coverage_scope(snapshot.subject_key, kind)
        rows = [asdict(record) for record in records]
        cursor = _cursor(kind, "r.")
        existing = connection.execute(
            f"""SELECT r.record_id, {cursor}, coalesce(c.fetched_at, r.fetched_at),
                coalesce(c.source_key, r.source_key)
            FROM base.{TABLES[kind]} r LEFT JOIN ops.fitbit_coverage c
              ON c.subject_key=r.subject_key AND c.data_type=?
             AND c.data_source_family=? AND c.aggregation_version=?
             AND {cursor} >= c.range_start AND {cursor} < c.range_end
            WHERE r.subject_key=? AND {_kind_filter(kind, "r.")} AND r.record_id IN
                (SELECT item.record_id FROM unnest(?) incoming(item))""",
            [*scope[1:], snapshot.subject_key, rows],
        ).fetchall()
        existing.extend(
            connection.execute(
                "SELECT record_id, NULL, fetched_at, source_key FROM ops.fitbit_deleted_record "
                "WHERE subject_key=? AND data_type=? "
                "AND record_id IN (SELECT item.record_id FROM unnest(?) incoming(item))",
                [snapshot.subject_key, kind, rows],
            ).fetchall()
        )
        protected: set[str] = set()
        moved: set[datetime] = set()
        for identity, prior_cursor, fetched, prior_key in existing:
            if (fetched, prior_key) > (snapshot.fetched_at, source_key):
                protected.add(identity)
            elif prior_cursor is not None and not any(
                start <= prior_cursor < end for start, end in accepted
            ):
                moved.add(prior_cursor)
        if moved:
            connection.execute(
                f"UPDATE ops.fitbit_coverage SET content_sha256='',source_sha256='' WHERE {_SCOPE} "
                "AND EXISTS (SELECT 1 FROM unnest(?::TIMESTAMPTZ[]) previous(cursor_at) "
                "WHERE range_start <= previous.cursor_at AND range_end > previous.cursor_at)",
                [*scope, sorted(moved)],
            )
        return protected

    def _update_deletions(
        self,
        connection: DuckDBPyConnection,
        accepted: list[tuple[datetime, datetime]],
        accepted_ids: list[str],
        *,
        protected: set[str],
        source_key: str,
    ) -> None:
        snapshot = self.snapshot
        kind = snapshot.window.data_type
        cursor = _cursor(kind)
        for start, end in accepted:
            connection.execute(
                f"""INSERT INTO ops.fitbit_deleted_record
                SELECT subject_key, ?, record_id, ?, ? FROM base.{TABLES[kind]}
                WHERE subject_key=? AND {_kind_filter(kind)} AND {cursor} >= ? AND {cursor} < ?
                  AND record_id NOT IN (SELECT unnest(?::VARCHAR[]))
                ON CONFLICT (subject_key, data_type, record_id) DO UPDATE SET
                    fetched_at=excluded.fetched_at, source_key=excluded.source_key""",
                [
                    kind,
                    snapshot.fetched_at,
                    source_key,
                    snapshot.subject_key,
                    start,
                    end,
                    [*accepted_ids, *protected],
                ],
            )
        if accepted_ids:
            connection.execute(
                "DELETE FROM ops.fitbit_deleted_record WHERE subject_key=? AND data_type=? "
                "AND record_id IN (SELECT unnest(?::VARCHAR[]))",
                [snapshot.subject_key, kind, accepted_ids],
            )

    def _replace_rows(
        self,
        connection: DuckDBPyConnection,
        accepted: list[tuple[datetime, datetime]],
        protected: set[str],
        *,
        source_key: str,
        loaded_at: datetime,
    ) -> None:
        snapshot = self.snapshot
        kinds = (
            ("sleep", "sleep-stage", "sleep-wake")
            if snapshot.window.data_type == "sleep"
            else (snapshot.window.data_type,)
        )
        for kind in kinds:
            table = TABLES[kind]
            identity = "sleep_id" if kind in ("sleep-stage", "sleep-wake") else "record_id"
            cursor = _cursor(kind)
            for start, end in accepted:
                connection.execute(
                    f"DELETE FROM base.{table} WHERE subject_key=? AND {_kind_filter(kind)} "
                    f"AND {cursor} >= ? AND {cursor} < ? AND {identity} NOT IN (SELECT unnest(?::VARCHAR[]))",
                    [snapshot.subject_key, start, end, sorted(protected)],
                )
            rows = [
                asdict(record)
                for record in snapshot.records
                if record.kind == kind
                and any(start <= record.cursor < end for start, end in accepted)
                and (record.parent_id or record.record_id) not in protected
            ]
            if rows:
                fields, projection = _record_fields(kind)
                columns = (
                    "subject_key",
                    "record_id",
                    *fields,
                    "fetched_at",
                    "source_key",
                    "loaded_at",
                )
                updates = ", ".join(
                    f"{column}=excluded.{column}"
                    for column in columns
                    if column not in ("subject_key", "record_id", "metric", "kind")
                )
                # Parameter ingestion batches a day's rows into one statement.
                connection.execute(
                    f"""INSERT INTO base.{table} ({", ".join(columns)})
                    SELECT ?, r.record_id::VARCHAR, {projection}, ?, ?, ? FROM unnest(?) incoming(r)
                    ON CONFLICT DO UPDATE SET {updates}
                    WHERE (excluded.fetched_at, excluded.source_key) >= ({table}.fetched_at, {table}.source_key)""",
                    [snapshot.subject_key, snapshot.fetched_at, source_key, loaded_at, rows],
                )


@dataclass(frozen=True, slots=True)
class FitbitMinuteBatch:
    snapshot: HeartRateMinuteSnapshot

    def write_snapshot(
        self, connection: DuckDBPyConnection, *, source_key: str, loaded_at: datetime
    ) -> None:
        snapshot = self.snapshot
        window = snapshot.window
        scope = coverage_scope(snapshot.subject_key, window.data_type, snapshot.aggregation_version)
        coverage = _read_coverage(connection, scope, window)
        accepted = _accepted_ranges(snapshot, source_key, coverage)
        if not accepted:
            return
        digest = _digest(
            [asdict(row) for row in sorted(snapshot.minutes, key=lambda row: row.start)]
        )
        if not _unchanged(coverage, window, digest):
            for start, end in accepted:
                connection.execute(
                    "DELETE FROM base.fitbit_heart_rate_minute WHERE subject_key=? AND data_source_family=? "
                    "AND aggregation_version=? AND start_at>=? AND start_at<?",
                    [
                        snapshot.subject_key,
                        GOOGLE_WEARABLES,
                        snapshot.aggregation_version,
                        start,
                        end,
                    ],
                )
            incoming = [
                asdict(row)
                for row in snapshot.minutes
                if any(start <= row.start < end for start, end in accepted)
            ]
            if incoming:
                connection.execute(
                    """INSERT INTO base.fitbit_heart_rate_minute
                    SELECT ?, r.data_source_family::VARCHAR, r.start::TIMESTAMPTZ,
                        r.aggregation_version::VARCHAR, r.end::TIMESTAMPTZ,
                        r.average::DOUBLE, r.minimum::DOUBLE, r.maximum::DOUBLE,
                        r.sample_count::BIGINT, ?, ?, ? FROM unnest(?) incoming(r)""",
                    [snapshot.subject_key, snapshot.fetched_at, source_key, loaded_at, incoming],
                )
        source_digest = snapshot.source_sha256() if accepted == [(window.start, window.end)] else ""
        _replace_coverage(
            connection,
            scope,
            coverage,
            accepted,
            fetched_at=snapshot.fetched_at,
            source_key=source_key,
            digest=digest,
            source_digest=source_digest,
        )
