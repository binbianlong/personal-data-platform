"""Idempotent object-storage to MotherDuck loader job."""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any, Protocol

from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

from .models import LoadSummary, RawObject, SegmentDecodeError
from .parser import parse_segb_bytes

LOGGER = logging.getLogger(__name__)
RAW_PREFIX = "raw/screen_time/v1/"
LOADER_LEASE_SECONDS = 65 * 60


class JobAlreadyRunning(RuntimeError):
    """Raised when an unexpired warehouse lease belongs to another run."""


def raw_prefix_from_env() -> str:
    prefix = os.environ.get("B2_RAW_PREFIX", RAW_PREFIX).strip().strip("/")
    if not prefix or ".." in prefix.split("/"):
        raise ValueError("B2_RAW_PREFIX must be a non-empty normalized object prefix")
    return f"{prefix}/"


class RawRepository(Protocol):
    def list_raw(self, prefix: str = RAW_PREFIX) -> Iterable[Any]: ...

    def get_raw(self, key: str) -> bytes: ...

    def list_scan_receipts(self) -> Iterable[Any]: ...


def _normalize_ref(value: Any) -> RawObject:
    return RawObject(
        key=value.key,
        device_key=value.device_key,
        stream=value.stream,
        segment_key=value.segment_key,
        observed_at=value.observed_at,
        sha256=value.sha256,
    )


def _decompress_and_verify(raw: RawObject, stored: bytes) -> bytes:
    if not stored.startswith(b"\x1f\x8b"):
        raise SegmentDecodeError(f"raw object is not gzip encoded: {raw.key}")
    try:
        segment = gzip.decompress(stored)
    except gzip.BadGzipFile as error:
        raise SegmentDecodeError(f"raw object has invalid gzip data: {raw.key}") from error
    actual = hashlib.sha256(segment).hexdigest()
    if actual != raw.sha256:
        raise SegmentDecodeError(
            f"raw object checksum mismatch: {raw.key} expected={raw.sha256} actual={actual}"
        )
    return segment


def run_loader(
    repository: RawRepository, warehouse: Warehouse, *, prefix: str = RAW_PREFIX
) -> LoadSummary:
    """Load every not-yet-succeeded raw observation in a deterministic order."""

    refs = sorted(
        (_normalize_ref(value) for value in repository.list_raw(prefix)),
        key=lambda value: (value.observed_at, value.key),
    )
    if len({value.key for value in refs}) != len(refs):
        raise RuntimeError("raw object listing returned duplicate keys")

    already_loaded = warehouse.succeeded_keys()
    pending = [value for value in refs if value.key not in already_loaded]
    run_id = str(uuid.uuid4())
    if not warehouse.acquire_job_lock("loader", run_id, lease_seconds=LOADER_LEASE_SECONDS):
        raise JobAlreadyRunning("loader already has an unexpired job lease")
    succeeded = 0
    failed = 0
    record_count = 0
    job_started = False
    try:
        warehouse.begin_job("loader", run_id)
        job_started = True
        for raw in pending:
            byte_size = 0
            try:
                stored = repository.get_raw(raw.key)
                segment = _decompress_and_verify(raw, stored)
                byte_size = len(segment)
                records = parse_segb_bytes(raw, segment)
                record_count += warehouse.load_object(raw, byte_size=byte_size, records=records)
                succeeded += 1
            except Exception as error:
                failed += 1
                LOGGER.exception("failed to load raw object %s", raw.key)
                warehouse.mark_failed(raw, byte_size=byte_size, error=error)
        summary = LoadSummary(
            discovered=len(refs),
            skipped=len(refs) - len(pending),
            succeeded=succeeded,
            failed=failed,
            records=record_count,
        )
        warehouse.finish_job(run_id, succeeded=summary.ok, details=asdict(summary))
        return summary
    except Exception as error:
        if job_started:
            warehouse.finish_job(run_id, succeeded=False, details={"error": str(error)})
        raise
    finally:
        warehouse.release_job_lock("loader", run_id)


def run_loader_from_env() -> int:
    """Runtime entrypoint used by the Cloud Run loader job."""

    from personal_data_platform.storage.b2 import B2RawRepository

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    repository = B2RawRepository.from_env()
    warehouse = Warehouse(connect(WarehouseConfig.from_env()))
    try:
        warehouse.migrate()
        summary = run_loader(repository, warehouse, prefix=raw_prefix_from_env())
        LOGGER.info(
            "loader complete discovered=%d skipped=%d succeeded=%d failed=%d records=%d",
            summary.discovered,
            summary.skipped,
            summary.succeeded,
            summary.failed,
            summary.records,
        )
        return 0 if summary.ok else 1
    finally:
        warehouse.close()
