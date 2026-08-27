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
    collector_receipt_count: int
    stale_collector_count: int
    missing_collector_receipt_count: int
    missing_relations: tuple[str, ...] = ()
    failed_relation_queries: tuple[str, ...] = ()
    details: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"
