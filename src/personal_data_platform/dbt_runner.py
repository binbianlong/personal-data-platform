"""Run the versioned dbt models and their data-quality tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from personal_data_platform.config import temporary_environment
from personal_data_platform.network_diagnostics import network_phase
from personal_data_platform.storage.factory import WarehouseConfig, dbt_environment, open_warehouse

PROJECT_ROOT = Path(os.environ.get("PDP_PROJECT_ROOT", Path(__file__).resolve().parents[2]))
DBT_PROJECT_DIR = PROJECT_ROOT / "dbt"


def _invoke(arguments: list[str], *, timeout_seconds: float | None = None) -> None:
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise TimeoutError("dbt execution deadline exceeded")
        subprocess.run(
            [sys.executable, "-c", "from dbt.cli.main import cli; cli()", *arguments],
            check=True,
            timeout=timeout_seconds,
        )
        return
    try:
        from dbt.cli.main import dbtRunner
    except ImportError as error:  # pragma: no cover - packaging failure
        raise RuntimeError("dbt is required to run transformations") from error

    result = dbtRunner().invoke(arguments)
    if not result.success:
        raised = getattr(result, "exception", None)
        raise RuntimeError(f"dbt {' '.join(arguments)} failed") from raised


def run_dbt(
    *,
    target: str,
    project_dir: Path = DBT_PROJECT_DIR,
    selector: str | None = None,
    timeout_seconds: int | None = None,
    config: WarehouseConfig | None = None,
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
    with temporary_environment(dbt_environment(target=target, config=config)):
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        for operation in ("run", "test"):
            with network_phase("dbt_" + operation):
                if deadline is None:
                    _invoke([operation, *common])
                else:
                    _invoke([operation, *common], timeout_seconds=deadline - time.monotonic())


def run_dbt_from_env(
    *,
    source_id: str | None = None,
    stream: str | None = None,
    timeout_seconds: int = 50 * 60,
    config: WarehouseConfig | None = None,
) -> int:
    from personal_data_platform.sources.registry import get_dbt_selector

    selector = (
        get_dbt_selector(source_id, stream) if source_id is not None or stream is not None else None
    )
    target = os.environ.get("DBT_TARGET", "prod")
    if target != "prod":
        raise ValueError("Cloud dbt entrypoint requires DBT_TARGET=prod")

    config = config if config is not None else WarehouseConfig.from_env()
    warehouse = open_warehouse(config)
    try:
        warehouse.migrate()
    finally:
        warehouse.close()
    run_dbt(target=target, timeout_seconds=timeout_seconds, selector=selector, config=config)
    return 0
