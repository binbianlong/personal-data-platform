"""Warehouse operations shared by ingestion, recovery and runtime orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from personal_data_platform.raw.models import RawObject
    from personal_data_platform.sources.contracts import DecodedBatch
    from personal_data_platform.sources.fitbit.models import (
        HeartRateMinuteSnapshot,
        Snapshot,
        WriteResult,
    )


@dataclass(frozen=True, slots=True)
class IngestionState:
    object_key: str
    status: str


class WarehouseConnectionError(RuntimeError):
    """Reopen the warehouse before deciding whether a write committed."""


class WarehouseLeaseError(RuntimeError):
    """The shared writer lease no longer permits a write."""


class Warehouse(Protocol):
    @property
    def connection_usable(self) -> bool: ...

    def close(self) -> None: ...
    def migrate(self) -> None: ...
    def interrupt(self) -> None: ...

    def succeeded_keys_for(
        self, raw_objects: Iterable[RawObject], *, parser_version: str | None = None
    ) -> set[str]: ...

    def active_ingestion_states(
        self, *, source_id: str, stream: str
    ) -> dict[str, IngestionState]: ...

    def load_object(self, raw: RawObject, *, byte_size: int, batch: DecodedBatch) -> int: ...
    def mark_failed(self, raw: RawObject, *, byte_size: int, error: Exception) -> None: ...

    def commit_fitbit_snapshot(
        self,
        snapshot: Snapshot | HeartRateMinuteSnapshot,
        *,
        source_key: str,
        loaded_at: datetime,
        owner_id: str,
        deadline: float,
        monotonic: Callable[[], float],
    ) -> WriteResult: ...

    def acquire_job_lock(self, job_name: str, owner_id: str, *, lease_seconds: int) -> bool: ...
    def require_job_lock(self, owner_id: str, *, remaining_seconds: int = 1) -> None: ...
    def release_job_lock(self, job_name: str, owner_id: str) -> None: ...
    def begin_job(self, job_name: str, run_id: str) -> None: ...
    def finish_job(self, run_id: str, *, succeeded: bool, details: dict[str, object]) -> None: ...

    def complete_daily_job(
        self, run_id: str, *, details: dict[str, object], now: datetime, deadline: float
    ) -> None: ...

    def finish_collection(
        self, owner_id: str, *, heartbeats: list[tuple[str, dict[str, object]]]
    ) -> None: ...

    def prune_biome_receipts(
        self, live_raw: Iterable[RawObject], *, source_id: str, parser_version: str
    ) -> None: ...

    def get_heartbeat(self, monitor_name: str) -> tuple[datetime, str] | None: ...
    def current_biome_object_keys(self, *, source_id: str, stream: str) -> set[str]: ...
    def relation_names(self) -> set[str]: ...
    def check_relation(self, relation: str) -> None: ...
    def latest_completed_target_date(self) -> date | None: ...
    def require_empty_target(self) -> None: ...
    def probe(self) -> dict[str, object]: ...
