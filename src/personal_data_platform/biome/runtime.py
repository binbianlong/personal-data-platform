"""Serialized local collection and ordered, fenced Screen Time delivery."""

from __future__ import annotations

import gzip
import json
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast

from personal_data_platform.loader.job import LOADER_LEASE_SECONDS, JobAlreadyRunning
from personal_data_platform.sources.contracts import SourceAdapter
from personal_data_platform.storage.factory import WarehouseConfig, open_warehouse
from personal_data_platform.storage.warehouse import Warehouse

from .collector import CollectorSourceError, ScreenTimeCollector
from .collector_state import CollectorState
from .motherduck import BiomeBatch, adopted_records


def _ensure_initialized(state: CollectorState, warehouse: Warehouse) -> None:
    with state._connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            return
        if version != 0 or warehouse.has_screen_time_events(source_id=state.source_id):
            raise RuntimeError(
                "Screen Time local reference bootstrap must be verified before collection"
            )
        connection.execute("PRAGMA user_version = 1")


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
        run_id = str(uuid.uuid4())
        started = time.monotonic()
        result = "failed"
        stage = "lock"
        details: dict[str, object] = {}
        try:
            with state.lock():
                stage = "connection"
                warehouse = open_warehouse(warehouse_config())
                try:
                    stage = "migration"
                    if not schema_initialized:
                        warehouse.migrate()
                        schema_initialized = True
                    stage = "bootstrap"
                    with _warehouse_lease(warehouse):
                        _ensure_initialized(state, warehouse)
                    stage = "collection"
                    stats = collector.collect_once(parser_version=source.parser_version)
                    details.update(
                        devices=stats.devices,
                        segments=stats.segments,
                        archived=stats.archived,
                        skipped=stats.skipped,
                        deferred=stats.deferred,
                    )
                    stage = "delivery"
                    details["loaded"] = _load_pending(state, warehouse, source=source)
                    if stats.deferred:
                        raise CollectorSourceError(
                            f"{stats.deferred} incomplete Screen Time snapshots deferred"
                        )
                    result = "succeeded"
                finally:
                    warehouse.close()
        except JobAlreadyRunning:
            result = "deferred"
            raise
        finally:
            if stage == "lock" and result == "failed":
                result = "deferred"
            print(
                json.dumps(
                    {
                        "event": "screen_time_collection",
                        "run_id": run_id,
                        "source_id": source.source_id,
                        "result": result,
                        "stage": stage,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "duration_seconds": time.monotonic() - started,
                        **details,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    if not watch:
        collect_and_load()
        return 0
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
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        return 0


def load_pending(state: CollectorState, warehouse: Warehouse, *, source: SourceAdapter) -> None:
    """Drain under the same process lock used by collection."""
    with state.lock():
        _load_pending(state, warehouse, source=source)


def _load_pending(state: CollectorState, warehouse: Warehouse, *, source: SourceAdapter) -> int:
    if state.source_id != source.source_id:
        raise ValueError("collector state belongs to a different source")
    loaded = 0
    with _warehouse_lease(warehouse) as (owner, deadline):
        _ensure_initialized(state, warehouse)
        state.cleanup_sent_payloads()
        for pending in state.pending(stream=source.stream):
            if time.monotonic() >= deadline:
                raise TimeoutError("Screen Time delivery deadline exceeded")
            identity = pending.identity
            state.pin_parser(identity.object_key, source.parser_version)
            raw = source.parse_raw_key(
                identity.object_key, storage_created_at=identity.observed_at, storage_generation=1
            )
            batch = source.decode(raw, gzip.decompress(pending.compressed_payload))
            if batch.parser_version != source.parser_version:
                raise RuntimeError("decoded parser version does not match pinned pending version")
            records = adopted_records(cast(BiomeBatch, batch))
            keys = [r.event_key for r in records if r.event_key is not None]
            invalidated = state.invalidated_event_keys(raw.key, keys)
            warehouse.commit_screen_time_snapshot(
                raw, batch, invalidated_event_keys=invalidated, owner_id=owner, deadline=deadline
            )
            # Exceptions, including an unknown COMMIT outcome, leave this payload pending.
            state.mark_loaded(raw.key, event_keys=keys)
            loaded += 1
        return loaded


@contextmanager
def _warehouse_lease(warehouse: Warehouse) -> Iterator[tuple[str, float]]:
    owner = str(uuid.uuid4())
    if not warehouse.acquire_job_lock("loader", owner, lease_seconds=LOADER_LEASE_SECONDS):
        raise JobAlreadyRunning("loader already has an unexpired job lease")
    try:
        yield owner, time.monotonic() + 20 * 60
    finally:
        if warehouse.connection_usable:
            failure = sys.exception()
            try:
                warehouse.release_job_lock("loader", owner)
            except Exception:
                if failure is None:
                    raise
                failure.add_note("loader lease cleanup also failed")
