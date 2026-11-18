"""Idempotent object-storage to MotherDuck loader job."""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import time
import zlib
from collections.abc import Callable, Iterable, Mapping

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import (
    RawRepository,
    SourceAdapter,
    list_source_raw,
    validate_observations,
    validate_runtime_policy,
)
from personal_data_platform.sources.registry import get_source, get_sources
from personal_data_platform.storage.factory import open_warehouse
from personal_data_platform.storage.warehouse import Warehouse, WarehouseConnectionError

from .deadline import interrupt_after
from .models import LoadSummary, RawDecodeError

LOGGER = logging.getLogger(__name__)
LOADER_LEASE_SECONDS = 125 * 60


class JobAlreadyRunning(RuntimeError):
    """Raised when an unexpired warehouse lease belongs to another run."""


def _decompress_and_verify(raw: RawObject, stored: bytes) -> bytes:
    if not stored.startswith(b"\x1f\x8b"):
        raise RawDecodeError(f"raw object is not gzip encoded: {raw.key}")
    try:
        payload = gzip.decompress(stored)
    except (gzip.BadGzipFile, EOFError, zlib.error) as error:
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
    reprocess: bool = False,
    _deadline: float | None = None,
) -> LoadSummary:
    """Load pending observations for one source and stream across supported schemas."""
    source = source or get_source()
    refs = list_source_raw(repository, source, prefix)
    return run_loader_objects(
        repository,
        warehouse,
        refs,
        source=source,
        reprocess=reprocess,
        _deadline=_deadline,
    )


def run_loader_objects(
    repository: RawRepository,
    warehouse: Warehouse,
    observations: Iterable[RawObject],
    *,
    source: SourceAdapter,
    reprocess: bool = False,
    buffered_payloads: Mapping[str, bytes] | None = None,
    _deadline: float | None = None,
) -> LoadSummary:
    """Load only generation-pinned references; never list storage or run migrations."""
    if source.source_id != "owntracks":
        raise ValueError(
            "object loader supports OwnTracks only; Screen Time uses its local pending queue"
        )
    refs = validate_observations(source, observations)
    deadline = time.monotonic() + 50 * 60
    if _deadline is not None:
        deadline = min(deadline, _deadline)
    succeeded = 0
    failed = 0
    record_count = 0

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("loader execution deadline exceeded")

    check_deadline()
    timer = interrupt_after(warehouse, deadline - time.monotonic())
    try:
        already_loaded = (
            set() if reprocess else warehouse.location_point_ids(raw.logical_key for raw in refs)
        )
        pending = [raw for raw in refs if raw.logical_key not in already_loaded]
        for raw in pending:
            check_deadline()
            try:
                stored = (
                    buffered_payloads[raw.key]
                    if buffered_payloads is not None and raw.key in buffered_payloads
                    else repository.get_raw(raw.key, generation=raw.storage_generation)
                )
                payload = _decompress_and_verify(raw, stored)
                batch = source.decode(raw, payload)
                check_deadline()
                record_count += warehouse.load_object(
                    raw, batch=batch, reprocess=reprocess, deadline=deadline
                )
                succeeded += 1
            except (WarehouseConnectionError, TimeoutError):
                raise
            except Exception:
                check_deadline()
                failed += 1
                LOGGER.exception("failed to load raw object %s", raw.key)
        summary = LoadSummary(
            discovered=len(refs),
            skipped=len(refs) - len(pending),
            succeeded=succeeded,
            failed=failed,
            records=record_count,
        )
        check_deadline()
        return summary
    finally:
        timer.cancel()
        timer.join()


def run_loader_all(
    sources: Iterable[SourceAdapter],
    *,
    warehouse: Warehouse,
    repository_factory: Callable[[SourceAdapter], RawRepository],
) -> LoadSummary:
    """Attempt every registered stream while the connection remains usable."""
    totals = LoadSummary(discovered=0, skipped=0, succeeded=0, failed=0, records=0)
    for source in sources:
        try:
            summary = run_loader(repository_factory(source), warehouse, source=source)
        except Exception:
            LOGGER.exception("failed to load source=%s stream=%s", source.source_id, source.stream)
            totals = LoadSummary(
                discovered=totals.discovered,
                skipped=totals.skipped,
                succeeded=totals.succeeded,
                failed=totals.failed + 1,
                records=totals.records,
            )
            if not warehouse.connection_usable:
                break
            continue
        totals = LoadSummary(
            discovered=totals.discovered + summary.discovered,
            skipped=totals.skipped + summary.skipped,
            succeeded=totals.succeeded + summary.succeeded,
            failed=totals.failed + summary.failed,
            records=totals.records + summary.records,
        )
    return totals


def run_loader_from_env(
    *,
    source_id: str | None = None,
    stream: str | None = None,
    all_streams: bool = False,
    prefix: str | None = None,
    reprocess: bool = False,
) -> int:
    """Runtime entrypoint used by a source-scoped Cloud Run loader job."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    sources = (
        get_sources(source_id, stream, all_streams=True)
        if all_streams
        else (get_source(source_id, stream),)
    )
    for source in sources:
        validate_runtime_policy(source)
    if all_streams and (prefix is not None or reprocess):
        raise ValueError("prefix/reprocess requires a single source stream")
    warehouse = open_warehouse()
    try:
        warehouse.migrate()
        if all_streams:
            summary = run_loader_all(
                sources,
                warehouse=warehouse,
                repository_factory=lambda source: source.repository_from_env(),
            )
        else:
            summary = run_loader(
                sources[0].repository_from_env(),
                warehouse,
                source=sources[0],
                prefix=prefix,
                reprocess=reprocess,
            )
        LOGGER.info(
            "loader complete source=%s streams=%s discovered=%d skipped=%d "
            "succeeded=%d failed=%d records=%d",
            sources[0].source_id,
            ",".join(source.stream for source in sources),
            summary.discovered,
            summary.skipped,
            summary.succeeded,
            summary.failed,
            summary.records,
        )
        return 0 if summary.ok else 1
    finally:
        warehouse.close()
