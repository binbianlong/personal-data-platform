"""Idempotent object-storage to MotherDuck loader job."""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import uuid
from dataclasses import asdict

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import (
    RawRepository,
    SourceAdapter,
    list_source_raw,
    validate_runtime_policy,
)
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

from .models import LoadSummary, RawDecodeError

LOGGER = logging.getLogger(__name__)
LOADER_LEASE_SECONDS = 65 * 60


class JobAlreadyRunning(RuntimeError):
    """Raised when an unexpired warehouse lease belongs to another run."""


def _decompress_and_verify(raw: RawObject, stored: bytes) -> bytes:
    if not stored.startswith(b"\x1f\x8b"):
        raise RawDecodeError(f"raw object is not gzip encoded: {raw.key}")
    try:
        payload = gzip.decompress(stored)
    except gzip.BadGzipFile as error:
        raise RawDecodeError(f"raw object has invalid gzip data: {raw.key}") from error
    actual = hashlib.sha256(payload).hexdigest()
    if actual != raw.sha256:
        raise RawDecodeError(
            f"raw object checksum mismatch: {raw.key} expected={raw.sha256} actual={actual}"
        )
    return payload


def run_loader(
    repository: RawRepository,
    warehouse: Warehouse,
    *,
    source: SourceAdapter | None = None,
    prefix: str | None = None,
) -> LoadSummary:
    """Load pending observations for one source and stream across supported schemas."""
    source = source or get_source()
    refs = sorted(
        list_source_raw(repository, source, prefix), key=lambda raw: (raw.observed_at, raw.key)
    )
    run_id = str(uuid.uuid4())
    # All source streams share one warehouse write lease. Source selection does not
    # change the database's concurrency contract.
    if not warehouse.acquire_job_lock("loader", run_id, lease_seconds=LOADER_LEASE_SECONDS):
        raise JobAlreadyRunning("loader already has an unexpired job lease")
    succeeded = 0
    failed = 0
    record_count = 0
    job_started = False
    scope_details = {"source_id": source.source_id, "stream": source.stream}
    try:
        warehouse.begin_job(f"loader:{source.source_id}:{source.stream}", run_id)
        job_started = True
        if source.source_id == "screen_time" and source.stream == "app-in-focus":
            factory = getattr(repository, "checkpoint_store", None)
            store = factory(warehouse) if factory is not None else None
            warehouse.open_screen_time_ingestion(store)
        already_loaded = warehouse.succeeded_keys_for(
            refs, parser_version=getattr(source, "parser_version", None)
        )
        pending = [raw for raw in refs if raw.key not in already_loaded]
        for raw in pending:
            byte_size = 0
            legacy_scope = source.legacy_scope(raw)
            try:
                stored = repository.get_raw(raw.key, generation=raw.storage_generation)
                payload = _decompress_and_verify(raw, stored)
                byte_size = len(payload)
                batch = source.decode(raw, payload)
                record_count += warehouse.load_object(
                    raw, byte_size=byte_size, batch=batch, legacy_scope=legacy_scope
                )
                succeeded += 1
            except Exception as error:
                from personal_data_platform.sources.screen_time.ingestion import CheckpointError

                if isinstance(error, CheckpointError):
                    raise
                failed += 1
                LOGGER.exception("failed to load raw object %s", raw.key)
                warehouse.mark_failed(
                    raw, byte_size=byte_size, error=error, legacy_scope=legacy_scope
                )
        summary = LoadSummary(
            discovered=len(refs),
            skipped=len(refs) - len(pending),
            succeeded=succeeded,
            failed=failed,
            records=record_count,
        )
        warehouse.finish_job(
            run_id, succeeded=summary.ok, details={**scope_details, **asdict(summary)}
        )
        return summary
    except Exception as error:
        if job_started:
            warehouse.finish_job(
                run_id, succeeded=False, details={**scope_details, "error": str(error)}
            )
        raise
    finally:
        warehouse.release_job_lock("loader", run_id)


def run_loader_from_env(*, source_id: str | None = None, stream: str | None = None) -> int:
    """Runtime entrypoint used by a source-scoped Cloud Run loader job."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    source = get_source(source_id, stream)
    validate_runtime_policy(source)
    repository = source.repository_from_env()
    warehouse = Warehouse(connect(WarehouseConfig.from_env()))
    try:
        warehouse.migrate()
        summary = run_loader(repository, warehouse, source=source)
        LOGGER.info(
            "loader complete source=%s stream=%s discovered=%d skipped=%d "
            "succeeded=%d failed=%d records=%d",
            source.source_id,
            source.stream,
            summary.discovered,
            summary.skipped,
            summary.succeeded,
            summary.failed,
            summary.records,
        )
        return 0 if summary.ok else 1
    finally:
        warehouse.close()
