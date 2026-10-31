"""Local Screen Time collection and warehouse loading operations."""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from personal_data_platform.loader.job import (
    LOADER_LEASE_SECONDS,
    JobAlreadyRunning,
    run_loader_objects,
)
from personal_data_platform.sources.contracts import SourceAdapter, list_source_raw
from personal_data_platform.storage.factory import WarehouseConfig, open_warehouse
from personal_data_platform.storage.warehouse import Warehouse

from .collector import CollectionStats, CollectorSourceError, ScreenTimeCollector
from .collector_state import CollectorState
from .storage import BiomeLocalRepository


def collect(
    collector: ScreenTimeCollector,
    state: CollectorState,
    *,
    source: SourceAdapter,
    warehouse_config: Callable[[], WarehouseConfig],
    poll_seconds: float,
    watch: bool,
) -> int:
    schema_initialized = False

    def collect_and_load() -> None:
        nonlocal schema_initialized
        stats = collector.collect_once()
        warehouse = open_warehouse(warehouse_config())
        try:
            if not schema_initialized:
                warehouse.migrate()
                schema_initialized = True
            pending = len(state.pending())
            load_pending(state, warehouse, source=source, publish_success=not stats.deferred)
            _print_collection_stats(stats)
            print(
                json.dumps(
                    {"loaded": pending - len(state.pending()), "pending": len(state.pending())}
                ),
                flush=True,
            )
        finally:
            warehouse.close()
        if stats.deferred:
            raise CollectorSourceError(
                f"{stats.deferred} incomplete Screen Time snapshots deferred"
            )

    if not watch:
        collect_and_load()
        return 0

    interval = poll_seconds
    try:
        while True:
            try:
                collect_and_load()
            except Exception as error:
                print(
                    f"Screen Time collection failed ({type(error).__name__}): {error}",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def load_pending(
    state: CollectorState,
    warehouse: Warehouse,
    *,
    source: SourceAdapter,
    now: datetime | None = None,
    publish_success: bool = True,
) -> None:
    """Confirm warehouse commits before local compaction or liveness publication."""
    completed_at = now or datetime.now(UTC)
    owner = str(uuid.uuid4())
    if not warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS):
        raise JobAlreadyRunning("loader already has an unexpired job lease")
    deadline = time.monotonic() + 20 * 60
    released = False
    try:
        repository = BiomeLocalRepository(state=state, source=source)
        pending_keys = {p.identity.object_key for p in state.pending(stream=source.stream)}
        refs = tuple(r for r in list_source_raw(repository, source) if r.key in pending_keys)
        if refs:
            summary = run_loader_objects(
                repository,
                warehouse,
                refs,
                source=source,
                _lease_owner=owner,
                _deadline=deadline,
            )
            for key in warehouse.succeeded_keys_for(refs, parser_version=source.parser_version):
                state.mark_loaded(key, completed_at)
            if not summary.ok:
                raise CollectorSourceError(f"{source.stream}: Raw ingestion failed")
        live_raw = tuple(list_source_raw(repository, source))
        health = source.audit(repository, live_raw, completed_at)
        if not health.ok or state.pending(stream=source.stream):
            raise CollectorSourceError(f"{source.stream}: collector audit failed")
        warehouse.prune_biome_receipts(
            live_raw, source_id=source.source_id, parser_version=source.parser_version
        )
        if publish_success:
            warehouse.finish_collection(
                owner,
                heartbeats=[
                    (
                        source.monitor_name,
                        {
                            **health.details,
                            "source_id": source.source_id,
                            "stream": source.stream,
                            "scan_completed_at": completed_at.isoformat(),
                        },
                    )
                ],
            )
            released = True
        else:
            warehouse.require_job_lock(owner)
        with state._connect() as connection:
            if connection.execute("PRAGMA freelist_count").fetchone()[0]:
                connection.execute("VACUUM")
    finally:
        if not released and warehouse.connection_usable:
            failure = sys.exception()
            try:
                warehouse.release_job_lock("loader", owner)
            except Exception:
                if failure is None:
                    raise
                failure.add_note("loader lease cleanup also failed")


def _print_collection_stats(stats: CollectionStats) -> None:
    print(
        json.dumps(
            {
                "devices": stats.devices,
                "segments": stats.segments,
                "archived": stats.archived,
                "skipped": stats.skipped,
                "deferred": stats.deferred,
            },
            sort_keys=True,
        ),
        flush=True,
    )
