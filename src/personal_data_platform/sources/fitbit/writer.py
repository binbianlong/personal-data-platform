"""Replace complete acquired ranges inside the warehouse's transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from personal_data_platform.raw.models import RawObject

from .models import PARSER_VERSION, TABLES, Record, Snapshot, Window

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from personal_data_platform.storage.motherduck import Warehouse


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
    origin: str
    source_key: str
    content_sha256: str
    source_sha256: str


def can_skip_snapshot(warehouse: Warehouse, snapshot: Snapshot) -> bool:
    """Omit Raw only for one exact, fully loaded, unchanged acquisition scope."""
    window = snapshot.window
    if warehouse.query_value(
        "SELECT count(*) FROM ops.fitbit_raw_intent WHERE subject_key=? AND data_type=?",
        [snapshot.subject_key, window.data_type],
    ):
        return False
    rows = warehouse.query_rows(
        "SELECT range_start, range_end, origin, source_sha256 "
        "FROM ops.fitbit_coverage WHERE subject_key=? AND data_type=? "
        "AND range_start < ? AND range_end > ?",
        [snapshot.subject_key, window.data_type, window.end, window.start],
    )
    return rows == [(window.start, window.end, snapshot.origin, snapshot.source_sha256())]


def clear_intents_for_loaded_raw(connection: DuckDBPyConnection, raw: RawObject) -> None:
    """Resolve this work's planned attempts no newer than a loaded Raw."""
    row = connection.execute(
        "SELECT receipt_key, work_index FROM ops.fitbit_raw_intent WHERE raw_key=?",
        [raw.key],
    ).fetchone()
    if row is not None:
        connection.execute(
            "DELETE FROM ops.fitbit_raw_intent WHERE receipt_key=? AND work_index=? "
            "AND fetched_at <= ?",
            [row[0], row[1], raw.observed_at],
        )


def _accepted_ranges(
    snapshot: Snapshot, source_key: str, coverage: list[_Coverage]
) -> list[tuple[datetime, datetime]]:
    """Exclude ranges already covered by a newer acquisition."""
    window = snapshot.window
    accepted = [(window.start, window.end)]
    for prior in coverage:
        protected = snapshot.origin == prior.origin and (prior.fetched_at, prior.source_key) > (
            snapshot.fetched_at,
            source_key,
        )
        if protected:
            accepted = _subtract(accepted, prior.start, prior.end)
    return accepted


def _content_digest(snapshot: Snapshot) -> str:
    records = [asdict(record) for record in snapshot.records]
    records.sort(
        key=lambda item: json.dumps(
            item, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    )
    return hashlib.sha256(
        json.dumps(
            {
                "origin": snapshot.origin,
                "records": records,
            },
            default=str,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class FitbitBatch:
    snapshot: Snapshot

    @property
    def parser_version(self) -> str:
        return PARSER_VERSION

    @property
    def record_count(self) -> int:
        return len(self.snapshot.records)

    def write(
        self,
        connection: DuckDBPyConnection,
        raw: RawObject,
        *,
        byte_size: int,
        loaded_at: datetime,
    ) -> None:
        self.write_snapshot(connection, source_key=raw.key, loaded_at=loaded_at)
        clear_intents_for_loaded_raw(connection, raw)

    def write_snapshot(
        self, connection: DuckDBPyConnection, *, source_key: str, loaded_at: datetime
    ) -> None:
        """Replace records and coverage inside the caller's transaction."""
        window = self.snapshot.window
        coverage = self._read_coverage(connection)
        accepted = _accepted_ranges(self.snapshot, source_key, coverage)
        if not accepted:
            return
        digest = _content_digest(self.snapshot)
        source_digest = self.snapshot.source_sha256()
        unchanged = (
            len(coverage) == 1
            and coverage[0].start == window.start
            and coverage[0].end == window.end
            and coverage[0].content_sha256 == digest
        )
        if not unchanged:
            protected_ids = self._replace_records(
                connection, accepted, source_key=source_key, loaded_at=loaded_at
            )
            if protected_ids:
                # Filtered stale identities no longer describe a complete payload.
                digest = ""
                source_digest = ""
        if accepted != [(window.start, window.end)]:
            source_digest = ""
        self._replace_coverage(
            connection,
            coverage,
            accepted,
            source_key=source_key,
            digest=digest,
            source_digest=source_digest,
        )

    def _read_coverage(self, connection: DuckDBPyConnection) -> list[_Coverage]:
        snapshot = self.snapshot
        window = snapshot.window
        rows = connection.execute(
            "SELECT range_start, range_end, fetched_at, origin, source_key, content_sha256, source_sha256 "
            "FROM ops.fitbit_coverage WHERE subject_key = ? AND data_type = ? "
            "AND range_start < ? AND range_end > ? ORDER BY range_start",
            [snapshot.subject_key, window.data_type, window.end, window.start],
        ).fetchall()
        return [_Coverage(*row) for row in rows]

    def _replace_records(
        self,
        connection: DuckDBPyConnection,
        accepted: list[tuple[datetime, datetime]],
        *,
        source_key: str,
        loaded_at: datetime,
    ) -> set[str]:
        snapshot = self.snapshot
        window = snapshot.window
        main_records = [
            record
            for record in snapshot.records
            if record.kind == window.data_type
            and any(start <= record.cursor < end for start, end in accepted)
        ]
        protected_ids = (
            self._protect_existing_records(
                connection, main_records, accepted, source_key=source_key
            )
            if main_records
            else set()
        )
        if main_records and window.data_type == "sleep":
            replaced = [
                record.record_id for record in main_records if record.record_id not in protected_ids
            ]
            for kind in ("sleep-stage", "sleep-wake"):
                connection.execute(
                    f"DELETE FROM base.{TABLES[kind]} WHERE subject_key=? "
                    "AND parent_id IN (SELECT unnest(?::VARCHAR[]))",
                    [snapshot.subject_key, replaced],
                )
        accepted_ids = [
            record.record_id for record in main_records if record.record_id not in protected_ids
        ]
        self._update_deletions(connection, accepted, accepted_ids, source_key=source_key)
        self._replace_rows(
            connection, accepted, protected_ids, source_key=source_key, loaded_at=loaded_at
        )
        return protected_ids

    def _protect_existing_records(
        self,
        connection: DuckDBPyConnection,
        records: list[Record],
        accepted: list[tuple[datetime, datetime]],
        *,
        source_key: str,
    ) -> set[str]:
        """Protect newer identities and invalidate coverage digests for moved records."""
        snapshot = self.snapshot
        window = snapshot.window
        protected_ids: set[str] = set()
        rows = [asdict(record) for record in records]
        existing = connection.execute(
            f"""SELECT r.record_id, r.cursor_at, r.origin,
                coalesce(c.fetched_at, r.fetched_at),
                coalesce(c.source_key, r.source_key)
            FROM base.{TABLES[window.data_type]} r
            LEFT JOIN ops.fitbit_coverage c
              ON c.subject_key=r.subject_key AND c.data_type=?
             AND r.cursor_at >= c.range_start AND r.cursor_at < c.range_end
            WHERE r.subject_key=? AND r.record_id IN
                (SELECT item.record_id FROM unnest(?) incoming(item))""",
            [window.data_type, snapshot.subject_key, rows],
        ).fetchall()
        existing.extend(
            connection.execute(
                "SELECT record_id, NULL, origin, fetched_at, source_key "
                "FROM ops.fitbit_deleted_record WHERE subject_key=? AND data_type=? "
                "AND record_id IN (SELECT item.record_id FROM unnest(?) incoming(item))",
                [snapshot.subject_key, window.data_type, rows],
            ).fetchall()
        )
        moved_cursors: set[datetime] = set()
        for identity, cursor, origin, fetched, prior_key in existing:
            if snapshot.origin == origin and (fetched, prior_key) > (
                snapshot.fetched_at,
                source_key,
            ):
                protected_ids.add(identity)
            elif cursor is not None and not any(start <= cursor < end for start, end in accepted):
                # A stable ID can move to another civil date. Its old
                # range no longer has the content cached by that digest.
                moved_cursors.add(cursor)
        if moved_cursors:
            connection.execute(
                "UPDATE ops.fitbit_coverage SET content_sha256='', source_sha256='' "
                "WHERE subject_key=? AND data_type=? AND EXISTS "
                "(SELECT 1 FROM unnest(?::TIMESTAMPTZ[]) previous(cursor_at) "
                " WHERE range_start <= previous.cursor_at AND range_end > previous.cursor_at)",
                [snapshot.subject_key, window.data_type, sorted(moved_cursors)],
            )
        return protected_ids

    def _update_deletions(
        self,
        connection: DuckDBPyConnection,
        accepted: list[tuple[datetime, datetime]],
        accepted_ids: list[str],
        *,
        source_key: str,
    ) -> None:
        snapshot = self.snapshot
        window = snapshot.window
        # Deletions have no live row on which to retain ordering. Preserve
        # only absent IDs, including IDs moved from another date earlier.
        for start, end in accepted:
            connection.execute(
                f"""INSERT INTO ops.fitbit_deleted_record
                SELECT subject_key, ?, record_id, ?, ?, ?
                FROM base.{TABLES[window.data_type]}
                WHERE subject_key=? AND cursor_at >= ? AND cursor_at < ?
                  AND record_id NOT IN (SELECT unnest(?::VARCHAR[]))
                ON CONFLICT (subject_key, data_type, record_id) DO UPDATE SET
                    fetched_at=excluded.fetched_at, origin=excluded.origin,
                    source_key=excluded.source_key""",
                [
                    window.data_type,
                    snapshot.fetched_at,
                    snapshot.origin,
                    source_key,
                    snapshot.subject_key,
                    start,
                    end,
                    accepted_ids,
                ],
            )
        if accepted_ids:
            connection.execute(
                "DELETE FROM ops.fitbit_deleted_record WHERE subject_key=? AND data_type=? "
                "AND record_id IN (SELECT unnest(?::VARCHAR[]))",
                [snapshot.subject_key, window.data_type, accepted_ids],
            )

    def _replace_rows(
        self,
        connection: DuckDBPyConnection,
        accepted: list[tuple[datetime, datetime]],
        protected_ids: set[str],
        *,
        source_key: str,
        loaded_at: datetime,
    ) -> None:
        snapshot = self.snapshot
        window = snapshot.window
        kinds = (
            ("sleep", "sleep-stage", "sleep-wake")
            if window.data_type == "sleep"
            else (window.data_type,)
        )
        for kind in kinds:
            table = TABLES[kind]
            for start, end in accepted:
                connection.execute(
                    f"DELETE FROM base.{table} WHERE subject_key = ? "
                    "AND cursor_at >= ? AND cursor_at < ?",
                    [snapshot.subject_key, start, end],
                )
            rows = [
                asdict(record)
                for record in snapshot.records
                if record.kind == kind
                and any(start <= record.cursor < end for start, end in accepted)
                and (record.parent_id or record.record_id) not in protected_ids
            ]
            if rows:
                # Columnar parameter ingestion keeps multi-million-row imports off a
                # Python executemany loop. Only one day's records are retained.
                connection.execute(
                    f"""INSERT INTO base.{table}
                    SELECT ?, r.record_id::VARCHAR, r.cursor::TIMESTAMPTZ,
                        r.start::TIMESTAMPTZ, r.end::TIMESTAMPTZ, r.value::DOUBLE,
                        r.offset_seconds::INTEGER, r.end_offset_seconds::INTEGER,
                        r.source_date::DATE, r.parent_id::VARCHAR, r.category::VARCHAR,
                        r.is_main_sleep::BOOLEAN, ?, ?, ?, ?
                    FROM unnest(?) AS incoming(r)
                    ON CONFLICT (subject_key, record_id) DO UPDATE SET
                        cursor_at=excluded.cursor_at, start_at=excluded.start_at,
                        end_at=excluded.end_at, value=excluded.value,
                        offset_seconds=excluded.offset_seconds,
                        end_offset_seconds=excluded.end_offset_seconds,
                        source_date=excluded.source_date, parent_id=excluded.parent_id,
                        category=excluded.category, is_main_sleep=excluded.is_main_sleep,
                        origin=excluded.origin, fetched_at=excluded.fetched_at,
                        source_key=excluded.source_key, loaded_at=excluded.loaded_at
                    WHERE (excluded.origin='api' AND {table}.origin<>'api') OR
                        (excluded.origin={table}.origin AND
                         (excluded.fetched_at, excluded.source_key) >=
                         ({table}.fetched_at, {table}.source_key))""",
                    [
                        snapshot.subject_key,
                        snapshot.origin,
                        snapshot.fetched_at,
                        source_key,
                        loaded_at,
                        rows,
                    ],
                )

    def _replace_coverage(
        self,
        connection: DuckDBPyConnection,
        coverage: list[_Coverage],
        accepted: list[tuple[datetime, datetime]],
        *,
        source_key: str,
        digest: str,
        source_digest: str,
    ) -> None:
        snapshot = self.snapshot
        window = snapshot.window
        # Split coverage, including empty acquisitions, to protect newer subranges.
        for prior in coverage:
            start, end = prior.start, prior.end
            remaining = [(start, end)]
            for left, right in accepted:
                remaining = _subtract(remaining, left, right)
            if remaining == [(start, end)]:
                continue
            connection.execute(
                "DELETE FROM ops.fitbit_coverage WHERE subject_key=? AND data_type=? AND range_start=?",
                [snapshot.subject_key, window.data_type, start],
            )
            for left, right in remaining:
                connection.execute(
                    "INSERT INTO ops.fitbit_coverage VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        snapshot.subject_key,
                        window.data_type,
                        left,
                        right,
                        prior.fetched_at,
                        prior.origin,
                        prior.source_key,
                        # The remaining fragment was not fetched as a complete
                        # window, so its old whole-window digest is not reusable.
                        "",
                        "",
                    ],
                )
        for start, end in accepted:
            connection.execute(
                "INSERT INTO ops.fitbit_coverage VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    snapshot.subject_key,
                    window.data_type,
                    start,
                    end,
                    snapshot.fetched_at,
                    snapshot.origin,
                    source_key,
                    digest,
                    source_digest,
                ],
            )


def expand_window(warehouse: Warehouse, subject_key: str, window: Window) -> Window:
    """Include existing interval starts crossing a notification's physical bounds."""
    if window.data_type not in ("steps", "active-zone-minutes"):
        return window
    start = window.start.replace(second=0, microsecond=0)
    end = window.end.replace(second=0, microsecond=0)
    if end < window.end:
        end += timedelta(minutes=1)
    while True:
        row = warehouse.query_rows(
            f"SELECT min(start_at), max(end_at) FROM base.{TABLES[window.data_type]} "
            "WHERE subject_key=? AND start_at < ? AND end_at > ?",
            [subject_key, end, start],
        )[0]
        left = min(start, row[0]) if row[0] is not None else start
        right = max(end, row[1]) if row[1] is not None else end
        if (left, right) == (start, end):
            return Window(window.data_type, start, end)
        start, end = left, right
