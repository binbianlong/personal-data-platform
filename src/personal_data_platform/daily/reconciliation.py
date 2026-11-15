"""Audit and repair OwnTracks Raw objects during daily processing."""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from personal_data_platform.loader.job import run_loader_objects
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import (
    RawRepository,
    SourceAdapter,
    list_source_raw,
    selected_prefixes,
    validate_observations,
)
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.warehouse import Warehouse

LOGGER = logging.getLogger(__name__)


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


def check_required_relations(
    warehouse: Warehouse, required_relations: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    available = warehouse.relation_names()
    missing = tuple(sorted(set(required_relations) - available))
    failed = []
    for relation in required_relations:
        if relation in available:
            try:
                warehouse.check_relation(relation)
            except Exception:
                LOGGER.exception("required relation query failed: %s", relation)
                failed.append(relation)
    return missing, tuple(failed)


def reconcile_owntracks(
    repository: RawRepository,
    warehouse: Warehouse,
    *,
    source: SourceAdapter | None = None,
    prefix: str | None = None,
    repair_missing: bool = True,
    now: datetime | None = None,
    raw_objects: tuple[RawObject, ...] | None = None,
    _lease_owner: str | None = None,
) -> ReconciliationResult:
    """Compare complete GCS logical IDs directly with base, repairing missing points."""
    source = source or get_source("owntracks")
    if source.source_id != "owntracks":
        raise ValueError(
            "Raw reconciliation supports OwnTracks; Screen Time history is canonical in base"
        )
    if selected_prefixes(source, prefix) != source.raw_prefixes:
        raise ValueError("reconciliation must audit every canonical prefix for the source stream")
    started_at = now or datetime.now(UTC)
    if started_at.tzinfo is None:
        raise ValueError("reconciliation time must be timezone-aware")
    if _lease_owner is not None:
        warehouse.require_job_lock(_lease_owner)
    refs = (
        list(validate_observations(source, raw_objects))
        if raw_objects is not None
        else list_source_raw(repository, source, prefix)
    )
    raw_ids = {raw.logical_key for raw in refs}
    loaded_ids = warehouse.location_point_ids()
    missing_before = raw_ids - loaded_ids
    repair_summary = None
    if missing_before and repair_missing:
        summary = run_loader_objects(
            repository, warehouse, refs, source=source, _lease_owner=_lease_owner
        )
        repair_summary = asdict(summary)
    loaded_ids = warehouse.location_point_ids()
    # A worker may have loaded an upload newer than the initial inventory.
    # Refresh only these candidate orphan IDs, without reconstructing GCS keys.
    if raw_objects is None and loaded_ids - raw_ids:
        raw_ids |= {
            raw.logical_key
            for raw in list_source_raw(repository, source, prefix)
            if raw.logical_key in loaded_ids
        }
    missing, orphaned = raw_ids - loaded_ids, loaded_ids - raw_ids
    failed = int(repair_summary["failed"]) if repair_summary else 0
    health = source.audit(repository, refs, started_at)
    missing_relations, failed_queries = check_required_relations(
        warehouse, source.required_relations
    )
    ok = health.ok and not (missing or orphaned or failed or missing_relations or failed_queries)
    details: dict[str, object] = {
        **health.details,
        "source_id": source.source_id,
        "stream": source.stream,
        "missing_before_repair": len(missing_before),
        "repair_summary": repair_summary,
        "total_object_count": len(loaded_ids),
        "missing_raw_object_count": len(orphaned),
        "live_loaded_object_count": len(raw_ids & loaded_ids),
        "live_unloaded_object_count": len(missing),
        "orphaned_loaded_object_count": len(orphaned),
        "missing_relations": list(missing_relations),
        "failed_relation_queries": list(failed_queries),
    }
    return ReconciliationResult(
        run_id=str(uuid.uuid4()),
        status="succeeded" if ok else "failed",
        started_at=started_at,
        completed_at=datetime.now(UTC),
        raw_object_count=len(raw_ids),
        loaded_object_count=len(raw_ids & loaded_ids),
        missing_object_count=len(missing),
        failed_object_count=failed,
        orphaned_loaded_object_count=len(orphaned),
        missing_relations=missing_relations,
        failed_relation_queries=failed_queries,
        details=details,
    )
