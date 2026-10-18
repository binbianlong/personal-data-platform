"""Replace complete acquired ranges inside the warehouse's transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from .models import (
    CIVIL_RECORD_AGGREGATION_VERSION,
    GOOGLE_WEARABLES,
    RECORD_AGGREGATION_VERSION,
    TABLES,
    HeartRateMinuteSnapshot,
    Record,
    Snapshot,
    Window,
    aware,
    date_cursor,
)

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

_SCOPE = "subject_key=? AND data_type=? AND data_source_family=? AND aggregation_version=?"


@dataclass(frozen=True, slots=True)
class WriteResult:
    outcome: Literal["range_replaced", "unchanged", "stale"]
    submitted_rows: int
    accepted_ranges: tuple[tuple[datetime, datetime], ...]
    protected_records: int = 0


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


def _read_coverage(
    connection: DuckDBPyConnection, scope: tuple[str, str, str, str], window: Window
) -> list[_Coverage]:
    rows = connection.execute(
        "SELECT range_start,range_end,fetched_at,source_key,content_sha256 "
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
) -> None:
    # Split empty and nonempty acquisitions alike to protect newer subranges.
    deleted = []
    rows = []
    for prior in coverage:
        remaining = [(prior.start, prior.end)]
        for start, end in accepted:
            remaining = _subtract(remaining, start, end)
        if remaining == [(prior.start, prior.end)]:
            continue
        deleted.append(prior.start)
        for start, end in remaining:
            # A fragment is not a complete acquisition; its hash is not reusable.
            rows.append(
                {
                    "start": start,
                    "end": end,
                    "fetched_at": prior.fetched_at,
                    "source_key": prior.source_key,
                    "content_sha256": "",
                }
            )
    rows.extend(
        {
            "start": start,
            "end": end,
            "fetched_at": fetched_at,
            "source_key": source_key,
            "content_sha256": digest,
        }
        for start, end in accepted
    )
    if deleted:
        connection.execute(
            f"DELETE FROM ops.fitbit_coverage WHERE {_SCOPE} "
            "AND range_start IN (SELECT unnest(?::TIMESTAMPTZ[]))",
            [*scope, deleted],
        )
    if rows:
        # Keep the legacy source_sha256 column empty without changing the DB schema.
        connection.execute(
            "INSERT INTO ops.fitbit_coverage "
            "SELECT ?,?,?,?,r.start::TIMESTAMPTZ,r.end::TIMESTAMPTZ,r.fetched_at::TIMESTAMPTZ,"
            "r.source_key::VARCHAR,r.content_sha256::VARCHAR,'' "
            "FROM unnest(?) incoming(r)",
            [*scope, rows],
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

    def write_snapshot(
        self, connection: DuckDBPyConnection, *, source_key: str, loaded_at: datetime
    ) -> WriteResult:
        snapshot = self.snapshot
        window = snapshot.window
        version = (
            CIVIL_RECORD_AGGREGATION_VERSION
            if window.time_basis == "civil"
            else RECORD_AGGREGATION_VERSION
        )
        scope = coverage_scope(snapshot.subject_key, window.data_type, version)
        coverage = _read_coverage(connection, scope, window)
        accepted = _accepted_ranges(snapshot, source_key, coverage)
        if not accepted:
            return WriteResult("stale", 0, ())
        records = [asdict(record) for record in snapshot.records]
        records.sort(key=lambda item: json.dumps(item, default=str, sort_keys=True))
        digest = _digest(records)
        unchanged = _unchanged(coverage, window, digest)
        submitted = 0
        protected: set[str] = set()
        if window.data_type == "steps":
            protected, submitted, changed = self._replace_steps(
                connection, accepted, source_key=source_key, loaded_at=loaded_at
            )
            unchanged = bool(coverage) and not changed
            if protected:
                digest = ""
        elif not unchanged:
            protected, submitted = self._replace_records(
                connection, accepted, source_key=source_key, loaded_at=loaded_at
            )
            if protected:
                digest = ""
        _replace_coverage(
            connection,
            scope,
            coverage,
            accepted,
            fetched_at=snapshot.fetched_at,
            source_key=source_key,
            digest=digest,
        )
        return WriteResult(
            "unchanged" if unchanged else "range_replaced",
            submitted,
            tuple(accepted),
            len(protected),
        )

    def _replace_steps(
        self,
        connection: DuckDBPyConnection,
        accepted: list[tuple[datetime, datetime]],
        *,
        source_key: str,
        loaded_at: datetime,
    ) -> tuple[set[str], int, bool]:
        snapshot = self.snapshot
        window = snapshot.window
        civil = window.time_basis == "civil"
        incoming = {
            record.record_id: record
            for record in snapshot.records
            if any(start <= window.record_cursor(record) < end for start, end in accepted)
        }
        cursor = "timezone('UTC', r.source_date::TIMESTAMP)" if civil else "r.start_at"
        rows = connection.execute(
            f"""SELECT r.record_id,r.start_at,r.end_at,r.value,r.offset_seconds,
                r.end_offset_seconds,r.source_date,r.category,r.fetched_at,r.source_key,
                c.fetched_at,c.source_key
            FROM base.fitbit_activity_interval r LEFT JOIN ops.fitbit_coverage c
              ON c.subject_key=r.subject_key AND c.data_type='steps'
             AND c.data_source_family=? AND (
                (c.aggregation_version=? AND r.start_at >= c.range_start AND r.start_at < c.range_end)
                OR (c.aggregation_version=? AND timezone('UTC', r.source_date::TIMESTAMP) >= c.range_start
                    AND timezone('UTC', r.source_date::TIMESTAMP) < c.range_end))
            WHERE r.subject_key=? AND r.metric='steps' AND
                (({cursor} >= ? AND {cursor} < ?)
                OR r.record_id IN (SELECT unnest(?::VARCHAR[]))
                OR (? AND r.source_date IS NULL))""",
            [
                GOOGLE_WEARABLES,
                RECORD_AGGREGATION_VERSION,
                CIVIL_RECORD_AGGREGATION_VERSION,
                snapshot.subject_key,
                snapshot.window.start,
                snapshot.window.end,
                sorted(incoming),
                civil,
            ],
        ).fetchall()
        if civil:
            for row in rows:
                if (
                    row[6] is None
                    or row[4] is None
                    or (aware(row[1]) + timedelta(seconds=row[4])).date() != row[6]
                ):
                    raise ValueError("stored steps have an ambiguous civil date")
        existing = {row[0]: row for row in rows}
        order = snapshot.fetched_at, source_key
        protected = {
            row[0]
            for row in rows
            if max((row[8], row[9]), (row[10], row[11]) if row[10] else (row[8], row[9])) > order
        }
        if incoming:
            protected.update(
                identity
                for identity, fetched, key in connection.execute(
                    "SELECT record_id,fetched_at,source_key FROM ops.fitbit_deleted_record "
                    "WHERE subject_key=? AND data_type='steps' "
                    "AND record_id IN (SELECT unnest(?::VARCHAR[]))",
                    [snapshot.subject_key, sorted(incoming)],
                ).fetchall()
                if (fetched, key) > order
            )
            if civil:
                other_window = Window(
                    "steps", window.start - timedelta(hours=18), window.end + timedelta(hours=18)
                )
                other_version = RECORD_AGGREGATION_VERSION
            else:
                other_start = date_cursor((window.start - timedelta(hours=18)).date())
                other_end = date_cursor((window.end + timedelta(hours=18)).date()) + timedelta(
                    days=1
                )
                other_window = Window("steps", other_start, other_end, time_basis="civil")
                other_version = CIVIL_RECORD_AGGREGATION_VERSION
            other_coverage = _read_coverage(
                connection,
                coverage_scope(snapshot.subject_key, "steps", other_version),
                other_window,
            )
            for identity, record in incoming.items():
                if not other_coverage:
                    continue
                cursor_at = record.cursor if civil else other_window.record_cursor(record)
                if any(
                    prior.start <= cursor_at < prior.end
                    and (prior.fetched_at, prior.source_key) > order
                    for prior in other_coverage
                ):
                    protected.add(identity)
        deleted = [
            identity
            for identity, row in existing.items()
            if identity not in incoming
            and identity not in protected
            and any(
                start <= (date_cursor(row[6]) if civil else row[1]) < end for start, end in accepted
            )
        ]
        changed = [
            record
            for identity, record in incoming.items()
            if identity not in protected
            and (
                identity not in existing
                or tuple(existing[identity][1:8])
                != (
                    record.start,
                    record.end,
                    record.value,
                    record.offset_seconds,
                    record.end_offset_seconds,
                    record.source_date,
                    record.category,
                )
            )
        ]
        moved = [
            existing[record.record_id][1] for record in changed if record.record_id in existing
        ]
        moved.extend(existing[identity][1] for identity in deleted)
        if civil:
            moved.extend(record.start for record in changed)
        if moved:
            connection.execute(
                f"UPDATE ops.fitbit_coverage SET content_sha256='' WHERE {_SCOPE} "
                "AND EXISTS (SELECT 1 FROM unnest(?::TIMESTAMPTZ[]) previous(cursor_at) "
                "WHERE range_start <= previous.cursor_at AND range_end > previous.cursor_at)",
                [*coverage_scope(snapshot.subject_key, "steps"), sorted(set(moved))],
            )
        if deleted:
            connection.execute(
                "INSERT INTO ops.fitbit_deleted_record "
                "SELECT ?, 'steps', unnest(?::VARCHAR[]), ?, ? "
                "ON CONFLICT (subject_key,data_type,record_id) DO UPDATE SET "
                "fetched_at=excluded.fetched_at,source_key=excluded.source_key",
                [snapshot.subject_key, deleted, snapshot.fetched_at, source_key],
            )
            connection.execute(
                "DELETE FROM base.fitbit_activity_interval WHERE subject_key=? AND metric='steps' "
                "AND record_id IN (SELECT unnest(?::VARCHAR[]))",
                [snapshot.subject_key, deleted],
            )
        accepted_ids = sorted(set(incoming) - protected)
        if accepted_ids:
            connection.execute(
                "DELETE FROM ops.fitbit_deleted_record WHERE subject_key=? AND data_type='steps' "
                "AND record_id IN (SELECT unnest(?::VARCHAR[]))",
                [snapshot.subject_key, accepted_ids],
            )
        if changed:
            fields, projection = _record_fields("steps")
            columns = ("subject_key", "record_id", *fields, "fetched_at", "source_key", "loaded_at")
            updates = ", ".join(
                f"{column}=excluded.{column}"
                for column in columns
                if column not in ("subject_key", "record_id", "metric")
            )
            connection.execute(
                f"INSERT INTO base.fitbit_activity_interval ({', '.join(columns)}) "
                f"SELECT ?, r.record_id::VARCHAR, {projection}, ?, ?, ? FROM unnest(?) incoming(r) "
                f"ON CONFLICT DO UPDATE SET {updates}",
                [
                    snapshot.subject_key,
                    snapshot.fetched_at,
                    source_key,
                    loaded_at,
                    [asdict(record) for record in changed],
                ],
            )
        return protected, len(changed), bool(changed or deleted)

    def _replace_records(
        self,
        connection: DuckDBPyConnection,
        accepted: list[tuple[datetime, datetime]],
        *,
        source_key: str,
        loaded_at: datetime,
    ) -> tuple[set[str], int]:
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
        submitted = self._replace_rows(
            connection, accepted, protected, source_key=source_key, loaded_at=loaded_at
        )
        return protected, submitted

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
        ids = [record.record_id for record in records]
        cursor = _cursor(kind, "r.")
        existing = connection.execute(
            f"""SELECT r.record_id, {cursor}, coalesce(c.fetched_at, r.fetched_at),
                coalesce(c.source_key, r.source_key)
            FROM base.{TABLES[kind]} r LEFT JOIN ops.fitbit_coverage c
              ON c.subject_key=r.subject_key AND c.data_type=?
             AND c.data_source_family=? AND c.aggregation_version=?
             AND {cursor} >= c.range_start AND {cursor} < c.range_end
            WHERE r.subject_key=? AND {_kind_filter(kind, "r.")} AND r.record_id IN
                (SELECT unnest(?::VARCHAR[]))""",
            [*scope[1:], snapshot.subject_key, ids],
        ).fetchall()
        existing.extend(
            connection.execute(
                "SELECT record_id, NULL, fetched_at, source_key FROM ops.fitbit_deleted_record "
                "WHERE subject_key=? AND data_type=? "
                "AND record_id IN (SELECT unnest(?::VARCHAR[]))",
                [snapshot.subject_key, kind, ids],
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
                f"UPDATE ops.fitbit_coverage SET content_sha256='' WHERE {_SCOPE} "
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
    ) -> int:
        snapshot = self.snapshot
        kinds = (
            ("sleep", "sleep-stage", "sleep-wake")
            if snapshot.window.data_type == "sleep"
            else (snapshot.window.data_type,)
        )
        submitted = 0
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
                submitted += len(rows)
        return submitted


@dataclass(frozen=True, slots=True)
class FitbitMinuteBatch:
    snapshot: HeartRateMinuteSnapshot

    def write_snapshot(
        self, connection: DuckDBPyConnection, *, source_key: str, loaded_at: datetime
    ) -> WriteResult:
        snapshot = self.snapshot
        window = snapshot.window
        scope = coverage_scope(snapshot.subject_key, window.data_type, snapshot.aggregation_version)
        coverage = _read_coverage(connection, scope, window)
        accepted = _accepted_ranges(snapshot, source_key, coverage)
        if not accepted:
            return WriteResult("stale", 0, ())
        digest = _digest(
            [asdict(row) for row in sorted(snapshot.minutes, key=lambda row: row.start)]
        )
        unchanged = _unchanged(coverage, window, digest)
        submitted = 0
        if not unchanged:
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
                submitted = len(incoming)
        _replace_coverage(
            connection,
            scope,
            coverage,
            accepted,
            fetched_at=snapshot.fetched_at,
            source_key=source_key,
            digest=digest,
        )
        return WriteResult(
            "unchanged" if unchanged else "range_replaced", submitted, tuple(accepted)
        )
