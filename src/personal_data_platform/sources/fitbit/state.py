"""Small warehouse checkpoints and write-ahead intents for the daily collector."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import cast

from personal_data_platform.storage.motherduck import Warehouse, WarehouseConnectionError

from .api import SyncTime
from .models import Window, object_dict, parse_time, string


@dataclass(frozen=True, slots=True)
class HistoricalRecheck:
    first: date
    stop: date
    expires: date
    cursor: date
    checked_on: date
    sync_time: str

    def __post_init__(self) -> None:
        if not self.first < self.stop or not self.first <= self.cursor <= self.stop:
            raise ValueError("invalid Fitbit historical recheck range")


@dataclass(frozen=True, slots=True)
class DailyState:
    subject_key: str
    completed_through: date | None = None
    sync_covered_through: date | None = None
    last_sync_time: SyncTime | None = None
    last_daily_run: date | None = None
    recheck: HistoricalRecheck | None = None


@dataclass(frozen=True, slots=True)
class CheckedWindow:
    window: Window
    fetched_at: datetime
    source_sha256: str


def _default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, SyncTime):
        return value.text
    raise TypeError("unsupported Fitbit checkpoint value")


def _window(value: object) -> Window:
    data = object_dict(value)
    return Window(
        string(data["data_type"]),
        parse_time(string(data["start"])),
        parse_time(string(data["end"])),
    )


def _date(value: object) -> date | None:
    return date.fromisoformat(string(value)) if value is not None else None


def _recheck(value: object) -> HistoricalRecheck | None:
    if value is None:
        return None
    data = object_dict(value)
    return HistoricalRecheck(
        date.fromisoformat(string(data["first"])),
        date.fromisoformat(string(data["stop"])),
        date.fromisoformat(string(data["expires"])),
        date.fromisoformat(string(data["cursor"])),
        date.fromisoformat(string(data["checked_on"])),
        string(data["sync_time"]),
    )


@dataclass(frozen=True, slots=True)
class PendingBatch:
    raw_key: str
    windows: tuple[Window, ...]
    skipped: tuple[CheckedWindow, ...]
    progress: DailyState
    supersedes: tuple[str, ...] = ()

    def to_json(self) -> str:
        progress = asdict(self.progress)
        progress["last_sync_time"] = (
            self.progress.last_sync_time.text if self.progress.last_sync_time else None
        )
        return json.dumps(
            {
                "windows": [asdict(window) for window in self.windows],
                "skipped": [asdict(value) for value in self.skipped],
                "progress": progress,
                "supersedes": self.supersedes,
            },
            default=_default,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw_key: str, payload: str) -> PendingBatch:
        data = object_dict(json.loads(payload))
        progress = object_dict(data["progress"])
        windows, skipped = data["windows"], data["skipped"]
        supersedes = data.get("supersedes", [])
        if not all(isinstance(value, list) for value in (windows, skipped, supersedes)):
            raise ValueError("Fitbit pending scopes must be arrays")
        return cls(
            raw_key,
            tuple(_window(value) for value in cast(list[object], windows)),
            tuple(
                CheckedWindow(
                    _window(item["window"]),
                    parse_time(string(item["fetched_at"])),
                    string(item["source_sha256"]),
                )
                for value in cast(list[object], skipped)
                for item in (object_dict(value),)
            ),
            DailyState(
                string(progress["subject_key"]),
                _date(progress["completed_through"]),
                _date(progress["sync_covered_through"]),
                SyncTime.parse(string(progress["last_sync_time"]))
                if progress["last_sync_time"] is not None
                else None,
                _date(progress["last_daily_run"]),
                _recheck(progress.get("recheck")),
            ),
            tuple(string(value) for value in cast(list[object], supersedes)),
        )


class DailyStateStore:
    def __init__(self, warehouse: Warehouse, subject_key: str) -> None:
        self.warehouse, self.subject_key = warehouse, subject_key

    def read(self) -> DailyState:
        rows = self.warehouse.query_rows(
            "SELECT completed_through, sync_covered_through, last_sync_time, last_daily_run, recheck_json "
            "FROM ops.fitbit_daily_state WHERE subject_key=?",
            [self.subject_key],
        )
        if not rows:
            return DailyState(self.subject_key)
        through, sync_through, sync_time, run_day, recheck_json = rows[0]
        return DailyState(
            self.subject_key,
            through,
            sync_through,
            SyncTime.parse(sync_time) if sync_time else None,
            run_day,
            _recheck(json.loads(recheck_json)) if recheck_json else None,
        )

    def pending(self) -> tuple[PendingBatch, ...]:
        values = tuple(
            PendingBatch.from_json(key, payload)
            for key, payload in self.warehouse.query_rows(
                "SELECT raw_key, details_json FROM ops.fitbit_batch_intent "
                "WHERE subject_key=? ORDER BY created_at, raw_key",
                [self.subject_key],
            )
        )
        if any(value.progress.subject_key != self.subject_key for value in values):
            raise ValueError("Fitbit pending batch subject does not match its checkpoint")
        return values

    def prepare(self, pending: PendingBatch) -> None:
        if pending.progress.subject_key != self.subject_key:
            raise ValueError("Fitbit pending batch belongs to another subject")
        payload = pending.to_json()
        self.warehouse.connection.execute(
            "INSERT INTO ops.fitbit_batch_intent VALUES (?,?,?,?) ON CONFLICT (raw_key) DO NOTHING",
            [pending.raw_key, self.subject_key, payload, datetime.now(UTC)],
        )
        if self.warehouse.query_rows(
            "SELECT subject_key, details_json FROM ops.fitbit_batch_intent WHERE raw_key=?",
            [pending.raw_key],
        ) != [(self.subject_key, payload)]:
            raise RuntimeError("Fitbit upload intent was not durably persisted")

    def finish(
        self,
        progress: DailyState,
        skipped: tuple[CheckedWindow, ...],
        *,
        retired_keys: tuple[str, ...] = (),
    ) -> None:
        """Advance verified coverage and checkpoints, then retire intents atomically."""
        if progress.subject_key != self.subject_key:
            raise ValueError("Fitbit progress belongs to another subject")
        connection = self.warehouse.connection
        commit_attempted = False
        try:
            connection.execute("BEGIN TRANSACTION")
            prior = self.read()

            def newest(left: date | None, right: date | None) -> date | None:
                return (
                    max(value for value in (left, right) if value is not None)
                    if left or right
                    else None
                )

            sync_time = max(
                (
                    value
                    for value in (prior.last_sync_time, progress.last_sync_time)
                    if value is not None
                ),
                default=None,
            )
            recheck = progress.recheck if progress.completed_through is not None else prior.recheck
            connection.execute(
                "INSERT INTO ops.fitbit_daily_state VALUES (?,?,?,?,?,?) "
                "ON CONFLICT (subject_key) DO UPDATE SET "
                "completed_through=excluded.completed_through, "
                "sync_covered_through=excluded.sync_covered_through, "
                "last_sync_time=excluded.last_sync_time, last_daily_run=excluded.last_daily_run, "
                "recheck_json=excluded.recheck_json",
                [
                    self.subject_key,
                    newest(prior.completed_through, progress.completed_through),
                    newest(prior.sync_covered_through, progress.sync_covered_through),
                    sync_time.text if sync_time else None,
                    newest(prior.last_daily_run, progress.last_daily_run),
                    json.dumps(asdict(recheck), default=_default, sort_keys=True)
                    if recheck
                    else None,
                ],
            )
            if skipped:
                connection.execute(
                    """
                    WITH checked AS (
                        SELECT item.window.data_type AS data_type,
                               item.window.start AS range_start, item.window.end AS range_end,
                               item.source_sha256 AS source_sha256, max(item.fetched_at) AS fetched_at
                        FROM unnest(?) checks(item)
                        GROUP BY ALL
                    )
                    UPDATE ops.fitbit_coverage AS coverage SET fetched_at=checked.fetched_at
                    FROM checked
                    WHERE coverage.subject_key=? AND coverage.data_type=checked.data_type
                      AND coverage.range_start=checked.range_start
                      AND coverage.range_end=checked.range_end
                      AND coverage.source_sha256=checked.source_sha256
                      AND coverage.fetched_at < checked.fetched_at
                    """,
                    [[asdict(item) for item in skipped], self.subject_key],
                )
            for key in retired_keys:
                connection.execute(
                    "DELETE FROM ops.fitbit_batch_intent WHERE raw_key=? AND subject_key=?",
                    [key, self.subject_key],
                )
                connection.execute(
                    "DELETE FROM ops.ingestion_metadata WHERE object_key=? AND status='failed'",
                    [key],
                )
            commit_attempted = True
            connection.execute("COMMIT")
        except Exception as error:
            if commit_attempted:
                self.warehouse.connection_usable = False
                raise WarehouseConnectionError(
                    "reopen warehouse after uncertain Fitbit checkpoint commit"
                ) from error
            try:
                connection.execute("ROLLBACK")
            except Exception as rollback_error:
                self.warehouse.connection_usable = False
                raise WarehouseConnectionError(
                    "cannot roll back Fitbit checkpoint transaction"
                ) from rollback_error
            raise
