"""Warehouse operations shared by ingestion, recovery and runtime orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from personal_data_platform.raw.models import RawObject
    from personal_data_platform.sources.contracts import DecodedBatch
    from personal_data_platform.sources.fitbit.models import (
        HeartRateMinuteSnapshot,
        Snapshot,
        WriteResult,
    )


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

    def location_point_ids(self, ids: Iterable[str] | None = None) -> set[str]: ...

    def load_object(
        self, raw: RawObject, *, batch: DecodedBatch, owner_id: str, deadline: float
    ) -> int: ...

    def commit_screen_time_snapshot(
        self,
        raw: RawObject,
        batch: DecodedBatch,
        *,
        invalidated_event_keys: Sequence[str],
        owner_id: str,
        deadline: float,
    ) -> int: ...

    def has_screen_time_events(self, *, source_id: str) -> bool: ...

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
    def fence_job_lock(self, owner_id: str, *, remaining_seconds: int = 1) -> None: ...
    def release_job_lock(self, job_name: str, owner_id: str) -> None: ...
    def relation_names(self) -> set[str]: ...
    def check_relation(self, relation: str) -> None: ...
    def require_empty_target(self) -> None: ...
    def probe(self) -> dict[str, object]: ...
