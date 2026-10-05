"""Idempotent object-storage to MotherDuck loader job."""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict

from personal_data_platform.config import schema_profile
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import (
    RawRepository,
    SourceAdapter,
    list_source_raw,
    validate_observations,
    validate_runtime_policy,
)
from personal_data_platform.sources.registry import get_source, get_sources
from personal_data_platform.storage.motherduck import (
    Warehouse,
    WarehouseConfig,
    WarehouseConnectionError,
    connect,
)

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
    _lease_owner: str | None = None,
    _deadline: float | None = None,
) -> LoadSummary:
    """Load pending observations for one source and stream across supported schemas."""
    source = source or get_source()
    refs = sorted(
        list_source_raw(repository, source, prefix), key=lambda raw: (raw.observed_at, raw.key)
    )
    return run_loader_objects(
        repository, warehouse, refs, source=source, _lease_owner=_lease_owner, _deadline=_deadline
    )


def run_loader_objects(
    repository: RawRepository,
    warehouse: Warehouse,
    observations: Iterable[RawObject],
    *,
    source: SourceAdapter,
    _lease_owner: str | None = None,
    buffered_payloads: Mapping[str, bytes] | None = None,
    _deadline: float | None = None,
) -> LoadSummary:
    """Load only generation-pinned references; never list storage or run migrations."""
    refs = validate_observations(source, observations)
    run_id = str(uuid.uuid4())
    # All source streams share one warehouse write lease. Source selection does not
    # change the database's concurrency contract.
    own_lease = _lease_owner is None
    west = schema_profile() == "west" or os.environ.get("PDP_FITBIT_DELIVERY_MODE") == "pubsub"
    deadline = _deadline if _deadline is not None else time.monotonic() + 50 * 60
    if own_lease and not warehouse.acquire_job_lock(
        "loader", run_id, lease_seconds=LOADER_LEASE_SECONDS
    ):
        raise JobAlreadyRunning("loader already has an unexpired job lease")
    succeeded = 0
    failed = 0
    record_count = 0
    job_started = False
    timer = None
    scope_details = {"source_id": source.source_id, "stream": source.stream}

    def guard() -> None:
        if west:
            seconds = int(deadline - time.monotonic())
            if seconds <= 0:
                raise TimeoutError("loader execution deadline exceeded")
            warehouse.require_job_lock(_lease_owner or run_id, remaining_seconds=seconds)

    try:
        guard()
        if west:
            timer = interrupt_after(warehouse, deadline - time.monotonic())
        warehouse.begin_job(f"loader:{source.source_id}:{source.stream}", run_id)
        job_started = True
        already_loaded = warehouse.succeeded_keys_for(refs, parser_version=source.parser_version)
        pending = [raw for raw in refs if raw.key not in already_loaded]
        from personal_data_platform.sources.fitbit.adapter import FitbitSource

        grouped: dict[str, list[RawObject]] = {}
        if isinstance(source, FitbitSource) and source.schema_versions == (2,):
            for raw in refs:
                grouped.setdefault(raw.logical_key.split(":")[0], []).append(raw)
            groups = [
                tuple(group)
                for group in grouped.values()
                if any(raw.key not in already_loaded for raw in group)
            ]
        else:
            groups = [(raw,) for raw in pending]
        for group in groups:
            guard()
            sizes: dict[str, int] = {}
            try:
                payloads = []
                for raw in group:
                    stored = (
                        buffered_payloads[raw.key]
                        if buffered_payloads is not None and raw.key in buffered_payloads
                        else repository.get_raw(raw.key, generation=raw.storage_generation)
                    )
                    if raw.source_id == "fitbit" and raw.schema_version == 2:
                        intent = warehouse.query_rows(
                            "SELECT compressed_sha256, compressed_size, storage_generation FROM ops.fitbit_bundle_chunk WHERE raw_key=?",
                            [raw.key],
                        )
                        if (
                            intent
                            and (
                                hashlib.sha256(stored).hexdigest(),
                                len(stored),
                                raw.storage_generation,
                            )
                            != intent[0]
                        ):
                            raise RawDecodeError(
                                "bundle compressed bytes or generation differ from intent"
                            )
                    payload = _decompress_and_verify(raw, stored)
                    sizes[raw.key] = len(payload)
                    payloads.append(payload)
                if isinstance(source, FitbitSource) and source.schema_versions == (2,):
                    batches = source.decode_bundle(group, tuple(payloads))
                    guard()
                    record_count += warehouse.load_objects(
                        (raw, sizes[raw.key], batch)
                        for raw, batch in zip(group, batches, strict=True)
                    )
                else:
                    raw = group[0]
                    batch = source.decode(raw, payloads[0])
                    guard()
                    record_count += warehouse.load_object(
                        raw, byte_size=sizes[raw.key], batch=batch
                    )
                succeeded += len(group)
            except (WarehouseConnectionError, TimeoutError):
                raise
            except Exception as error:
                guard()
                failed += len(group)
                LOGGER.exception("failed to load raw group %s", [raw.key for raw in group])
                for raw in group:
                    warehouse.mark_failed(raw, byte_size=sizes.get(raw.key, 0), error=error)
        summary = LoadSummary(
            discovered=len(refs),
            skipped=len(refs) - len(pending),
            succeeded=succeeded,
            failed=failed,
            records=record_count,
        )
        guard()
        warehouse.finish_job(
            run_id, succeeded=summary.ok, details={**scope_details, **asdict(summary)}
        )
        return summary
    except Exception as error:
        if job_started and warehouse.connection_usable:
            guard()
            warehouse.finish_job(
                run_id, succeeded=False, details={**scope_details, "error": str(error)}
            )
        raise
    finally:
        if timer is not None:
            timer.cancel()
        if own_lease and warehouse.connection_usable:
            warehouse.release_job_lock("loader", run_id)


def run_loader_all(
    sources: Iterable[SourceAdapter],
    *,
    warehouse: Warehouse,
    repository_factory: Callable[[SourceAdapter], RawRepository],
) -> LoadSummary:
    """Keep one loader lease while attempting every registered stream."""
    owner_id = str(uuid.uuid4())
    if not warehouse.acquire_job_lock("loader", owner_id, lease_seconds=LOADER_LEASE_SECONDS):
        raise JobAlreadyRunning("loader already has an unexpired job lease")
    totals = LoadSummary(discovered=0, skipped=0, succeeded=0, failed=0, records=0)
    try:
        for source in sources:
            try:
                summary = run_loader(
                    repository_factory(source), warehouse, source=source, _lease_owner=owner_id
                )
            except Exception:
                LOGGER.exception(
                    "failed to load source=%s stream=%s", source.source_id, source.stream
                )
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
    finally:
        if warehouse.connection_usable:
            warehouse.release_job_lock("loader", owner_id)


def run_loader_from_env(
    *, source_id: str | None = None, stream: str | None = None, all_streams: bool = False
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
    warehouse = Warehouse(connect(WarehouseConfig.from_env()))
    try:
        from personal_data_platform.config import schema_profile

        warehouse.migrate(profile=schema_profile())
        try:
            if all_streams:
                summary = run_loader_all(
                    sources,
                    warehouse=warehouse,
                    repository_factory=lambda source: source.repository_from_env(),
                )
            else:
                summary = run_loader(sources[0].repository_from_env(), warehouse, source=sources[0])
        except JobAlreadyRunning:
            LOGGER.info(
                "loader skipped source=%s: another run is active",
                sources[0].source_id,
            )
            return 0
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
