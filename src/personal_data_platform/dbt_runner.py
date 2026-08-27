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


def run_dbt(*, target: str, project_dir: Path = DBT_PROJECT_DIR) -> None:
    common = [
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(project_dir),
        "--target",
        target,
    ]
    _invoke(["run", *common])
    _invoke(["test", *common])


def run_dbt_from_env() -> int:
    target = os.environ.get("DBT_TARGET", "prod")
    if target != "prod":
        raise ValueError("Cloud dbt entrypoint requires DBT_TARGET=prod")

    warehouse = Warehouse(connect(WarehouseConfig.from_env()))
    try:
        warehouse.migrate()
    finally:
        warehouse.close()
    run_dbt(target=target)
    return 0
