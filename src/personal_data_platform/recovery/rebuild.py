"""Rebuild all immutable raw observations into an explicitly named scratch database."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.loader.job import (
    RAW_PREFIX,
    RawRepository,
    raw_prefix_from_env,
    run_loader,
)
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

_SAFE_DATABASE = re.compile(r"^[A-Za-z0-9_-]+$")


def rebuild_inventory(observations: Iterable[Any]) -> dict[str, object]:
    materialized = sorted(observations, key=lambda value: (value.observed_at, value.key))
    return {
        "raw_object_count": len(materialized),
        "device_count": len({value.device_key for value in materialized}),
        "segment_count": len(
            {(value.device_key, value.stream, value.segment_key) for value in materialized}
        ),
        "first_observed_at": (materialized[0].observed_at.isoformat() if materialized else None),
        "last_observed_at": (materialized[-1].observed_at.isoformat() if materialized else None),
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
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
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
    prefix: str = RAW_PREFIX,
) -> int:
    """Replay raw objects into a scratch database without clearing either database."""

    validate_rebuild_target(target_db, production_db)
    warehouse = Warehouse(connect(WarehouseConfig(database=target_db, token=token)))
    try:
        require_empty_rebuild_target(warehouse)
        warehouse.migrate()
        summary = run_loader(repository, warehouse, prefix=prefix)
    finally:
        warehouse.close()
    if not summary.ok:
        return 1
    with _temporary_environment("MOTHERDUCK_DATABASE", target_db):
        run_dbt(target="prod")
    return 0


def run_rebuild_from_env(*, dry_run: bool, target_db: str | None) -> int:
    from personal_data_platform.storage.b2 import B2RawRepository

    repository = B2RawRepository.from_env()
    prefix = raw_prefix_from_env()
    observations = repository.list_raw(prefix)
    inventory = rebuild_inventory(observations)
    print(json.dumps(inventory, sort_keys=True))
    if dry_run:
        return 0
    if target_db is None:
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
        prefix=prefix,
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
