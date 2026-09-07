"""Run the versioned dbt models and their data-quality tests."""

from __future__ import annotations

import os
from pathlib import Path

from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

PROJECT_ROOT = Path(os.environ.get("PDP_PROJECT_ROOT", Path(__file__).resolve().parents[2]))
DBT_PROJECT_DIR = PROJECT_ROOT / "dbt"


def _invoke(arguments: list[str]) -> None:
    try:
        from dbt.cli.main import dbtRunner
    except ImportError as error:  # pragma: no cover - packaging failure
        raise RuntimeError("dbt-duckdb is required to run transformations") from error

    result = dbtRunner().invoke(arguments)
    if not result.success:
        raised = getattr(result, "exception", None)
        raise RuntimeError(f"dbt {' '.join(arguments)} failed") from raised


def run_dbt(
    *, target: str, project_dir: Path = DBT_PROJECT_DIR, selector: str | None = None
) -> None:
    common = [
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(project_dir),
        "--target",
        target,
    ]
    if selector is not None:
        if not selector.strip():
            raise ValueError("dbt selector must not be empty")
        common.extend(["--select", selector])
    secret_name = "DBT_ENV_SECRET_MOTHERDUCK_TOKEN"
    previous_secret = os.environ.get(secret_name)
    if target == "prod":
        token = os.environ.get("MOTHERDUCK_TOKEN")
        if not token:
            raise ValueError("MOTHERDUCK_TOKEN is required for the production dbt target")
        os.environ[secret_name] = token
    try:
        _invoke(["run", *common])
        _invoke(["test", *common])
    finally:
        if target == "prod":
            if previous_secret is None:
                os.environ.pop(secret_name, None)
            else:
                os.environ[secret_name] = previous_secret


def run_dbt_from_env(*, source_id: str | None = None, stream: str | None = None) -> int:
    from personal_data_platform.sources.registry import get_source

    source = get_source(source_id, stream) if source_id is not None or stream is not None else None
    target = os.environ.get("DBT_TARGET", "prod")
    if target != "prod":
        raise ValueError("Cloud dbt entrypoint requires DBT_TARGET=prod")

    warehouse = Warehouse(connect(WarehouseConfig.from_env()))
    try:
        warehouse.migrate()
    finally:
        warehouse.close()
    run_dbt(target=target, **({"selector": source.dbt_selector} if source is not None else {}))
    return 0
