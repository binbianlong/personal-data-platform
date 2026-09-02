"""Rebuild all immutable raw observations into an explicitly named scratch database."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from personal_data_platform.config import RebuildADCConfig
from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.loader.job import (
    RAW_PREFIX,
    RawRepository,
    run_loader,
)
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

_SAFE_DATABASE = re.compile(r"^[A-Za-z0-9_-]+$")
RAW_RETENTION_DAYS = 90


@dataclass(frozen=True, slots=True)
class _SnapshotRawRepository:
    """Replay one inventory even if the bucket changes during the rebuild."""

    repository: RawRepository
    observations: tuple[Any, ...]

    def list_raw(self, prefix: str = RAW_PREFIX) -> Iterable[Any]:
        return self.observations

    def get_raw(self, key: str, *, generation: int) -> bytes:
        return self.repository.get_raw(key, generation=generation)

    def list_scan_receipts(self) -> Iterable[object]:
        return ()

    def get_device_manifest(self) -> object | None:
        return None


def rebuild_inventory(observations: Iterable[Any]) -> dict[str, object]:
    materialized = sorted(observations, key=lambda value: (value.observed_at, value.key))
    creation_times = sorted(value.storage_created_at for value in materialized)
    return {
        "raw_object_count": len(materialized),
        "device_count": len({value.device_key for value in materialized}),
        "segment_count": len(
            {(value.device_key, value.stream, value.segment_key) for value in materialized}
        ),
        "first_observed_at": (materialized[0].observed_at.isoformat() if materialized else None),
        "last_observed_at": (materialized[-1].observed_at.isoformat() if materialized else None),
        "first_storage_created_at": creation_times[0].isoformat() if creation_times else None,
        "last_storage_created_at": creation_times[-1].isoformat() if creation_times else None,
        "retention_days": RAW_RETENTION_DAYS,
        "full_history_rebuild_guaranteed": False,
    }


def validate_rebuild_target(target_db: str, production_db: str | None) -> None:
    if not _SAFE_DATABASE.fullmatch(target_db):
        raise ValueError(
            "rebuild target must contain only letters, digits, hyphens, and underscores"
        )
    if production_db and target_db.casefold() == production_db.casefold():
        raise ValueError("rebuild target must not be the production MotherDuck database")


def require_empty_rebuild_target(warehouse: Warehouse) -> None:
    table_count = warehouse.query_value(
        """
        SELECT count(*)
        FROM information_schema.tables
        WHERE table_catalog = current_database()
          AND table_schema NOT IN ('information_schema', 'pg_catalog')
        """
    )
    if table_count:
        raise ValueError("rebuild target must not contain existing tables or views")


def run_rebuild(
    repository: RawRepository,
    *,
    target_db: str,
    token: str,
    production_db: str | None,
    allow_partial_history: bool = False,
    prefix: str = RAW_PREFIX,
    observations: Iterable[Any] | None = None,
) -> int:
    """Replay currently retained Raw into a scratch database without clearing either database."""

    if not allow_partial_history:
        raise ValueError("--allow-partial-history is required for a retention-limited rebuild")
    validate_rebuild_target(target_db, production_db)
    warehouse = Warehouse(connect(WarehouseConfig(database=target_db, token=token)))
    try:
        require_empty_rebuild_target(warehouse)
        warehouse.migrate()
        snapshot = tuple(repository.list_raw(prefix) if observations is None else observations)
        summary = run_loader(
            _SnapshotRawRepository(repository=repository, observations=snapshot),
            warehouse,
            prefix=prefix,
        )
    finally:
        warehouse.close()
    if not summary.ok:
        return 1
    with (
        _temporary_environment("MOTHERDUCK_DATABASE", target_db),
        _temporary_environment("MOTHERDUCK_TOKEN", token),
    ):
        run_dbt(target="prod")
    return 0


def run_rebuild_from_env(
    *, dry_run: bool, target_db: str | None, allow_partial_history: bool = False
) -> int:
    from personal_data_platform.storage.gcs import GCSRawRepository

    if not dry_run:
        if target_db is None:
            raise ValueError("--target-db is required unless --dry-run is used")
        if not allow_partial_history:
            raise ValueError("--allow-partial-history is required when --target-db is used")
    adc = RebuildADCConfig.from_env()
    with _temporary_environment("GOOGLE_APPLICATION_CREDENTIALS", str(adc.credentials_path)):
        repository = GCSRawRepository.from_env()
        observations = repository.list_raw(RAW_PREFIX)
        inventory = rebuild_inventory(observations)
        print(json.dumps(inventory, sort_keys=True))
        if dry_run:
            return 0
        if target_db is None:  # guarded before the repository read; retained for type safety
            raise ValueError("--target-db is required unless --dry-run is used")
        token = os.environ.get("MOTHERDUCK_TOKEN")
        if not token:
            raise ValueError("MOTHERDUCK_TOKEN is required")
        production_db = os.environ.get("MOTHERDUCK_DATABASE")
        if not production_db:
            raise ValueError("MOTHERDUCK_DATABASE is required to protect the production target")
        return run_rebuild(
            repository,
            target_db=target_db,
            token=token,
            production_db=production_db,
            allow_partial_history=True,
            prefix=RAW_PREFIX,
            observations=observations,
        )


@contextmanager
def _temporary_environment(name: str, value: str):
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
