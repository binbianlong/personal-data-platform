"""Rebuild all immutable raw observations into an explicitly named scratch database."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass

from personal_data_platform.config import RebuildADCConfig
from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.loader.job import run_loader
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import (
    RawRepository,
    SourceAdapter,
    list_source_raw,
    selected_prefixes,
    validate_observations,
    validate_runtime_policy,
)
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

_SAFE_DATABASE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class _SnapshotRawRepository:
    """Replay one inventory even if the bucket changes during the rebuild."""

    repository: RawRepository
    observations: tuple[RawObject, ...]

    def list_raw(self, prefix: str) -> Iterable[RawObject]:
        return (value for value in self.observations if value.key.startswith(prefix))

    def get_raw(self, key: str, *, generation: int) -> bytes:
        return self.repository.get_raw(key, generation=generation)


def rebuild_inventory(
    observations: Iterable[RawObject], *, source: SourceAdapter | None = None
) -> dict[str, object]:
    source = source or get_source()
    materialized = sorted(
        validate_observations(source, observations),
        key=lambda value: (value.observed_at, value.key),
    )
    creation_times = sorted(value.storage_created_at for value in materialized)
    return {
        **source.inventory(materialized),
        "source_id": source.source_id,
        "stream": source.stream,
        "schema_versions": list(source.schema_versions),
        "raw_object_count": len(materialized),
        "subject_count": len({value.subject_key for value in materialized}),
        "scope_count": len(
            {(value.subject_key, value.stream, value.logical_key) for value in materialized}
        ),
        "first_observed_at": (materialized[0].observed_at.isoformat() if materialized else None),
        "last_observed_at": (materialized[-1].observed_at.isoformat() if materialized else None),
        "first_storage_created_at": creation_times[0].isoformat() if creation_times else None,
        "last_storage_created_at": creation_times[-1].isoformat() if creation_times else None,
        "retention_days": source.retention_days,
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
    source: SourceAdapter | None = None,
    prefix: str | None = None,
    observations: Iterable[RawObject] | None = None,
) -> int:
    """Replay currently retained Raw into a scratch database without clearing either database."""

    if not allow_partial_history:
        raise ValueError("--allow-partial-history is required for a retention-limited rebuild")
    validate_rebuild_target(target_db, production_db)
    source = source or get_source()
    prefixes = selected_prefixes(source, prefix)
    snapshot = tuple(
        list_source_raw(repository, source, prefix)
        if observations is None
        else validate_observations(source, observations)
    )
    if any(not value.key.startswith(prefixes) for value in snapshot):
        raise ValueError("rebuild inventory contains an object outside the requested prefix")
    warehouse = Warehouse(connect(WarehouseConfig(database=target_db, token=token)))
    try:
        require_empty_rebuild_target(warehouse)
        warehouse.migrate()
        summary = run_loader(
            _SnapshotRawRepository(repository=repository, observations=snapshot),
            warehouse,
            source=source,
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
        run_dbt(target="prod", selector=source.dbt_selector)
    return 0


def run_rebuild_from_env(
    *,
    dry_run: bool,
    target_db: str | None,
    allow_partial_history: bool = False,
    source_id: str | None = None,
    stream: str | None = None,
) -> int:

    if not dry_run:
        if target_db is None:
            raise ValueError("--target-db is required unless --dry-run is used")
        if not allow_partial_history:
            raise ValueError("--allow-partial-history is required when --target-db is used")
    source = get_source(source_id=source_id, stream=stream)
    validate_runtime_policy(source)
    adc = RebuildADCConfig.from_env()
    with _temporary_environment("GOOGLE_APPLICATION_CREDENTIALS", str(adc.credentials_path)):
        repository = source.repository_from_env()
        observations = list_source_raw(repository, source)
        inventory = rebuild_inventory(observations, source=source)
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
            source=source,
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
