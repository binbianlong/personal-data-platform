"""Result types for reconciliation runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    run_id: str
    status: str
    started_at: datetime
    completed_at: datetime
    raw_object_count: int
    loaded_object_count: int
    missing_object_count: int
    failed_object_count: int
    orphaned_loaded_object_count: int
    missing_relations: tuple[str, ...] = ()
    failed_relation_queries: tuple[str, ...] = ()
    details: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    @property
    def collector_receipt_count(self) -> int:
        return int(self.details.get("collector_receipt_count", 0))

    @property
    def stale_collector_count(self) -> int:
        return int(self.details.get("stale_collector_count", 0))

    @property
    def missing_collector_receipt_count(self) -> int:
        return int(self.details.get("missing_collector_receipt_count", 0))
