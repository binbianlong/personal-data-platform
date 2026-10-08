"""Run the versioned dbt models and their data-quality tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from personal_data_platform.loader.job import LOADER_LEASE_SECONDS, JobAlreadyRunning
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

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
        raise RuntimeError("dbt-duckdb is required to run transformations") from error

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
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        for operation in ("run", "test"):
            if deadline is None:
                _invoke([operation, *common])
            else:
                _invoke([operation, *common], timeout_seconds=deadline - time.monotonic())
    finally:
        if target == "prod":
            if previous_secret is None:
                os.environ.pop(secret_name, None)
            else:
                os.environ[secret_name] = previous_secret


def run_dbt_from_env(
    *,
    source_id: str | None = None,
    stream: str | None = None,
    lease_owner: str | None = None,
    timeout_seconds: int = 50 * 60,
) -> int:
    from personal_data_platform.sources.registry import get_dbt_selector

    selector = (
        get_dbt_selector(source_id, stream) if source_id is not None or stream is not None else None
    )
    target = os.environ.get("DBT_TARGET", "prod")
    if target != "prod":
        raise ValueError("Cloud dbt entrypoint requires DBT_TARGET=prod")

    warehouse = Warehouse(connect(WarehouseConfig.from_env()))
    owner = lease_owner or str(uuid.uuid4())
    acquired = False
    try:
        warehouse.migrate()
        if lease_owner is None:
            acquired = warehouse.acquire_job_lock(
                "loader", owner, lease_seconds=LOADER_LEASE_SECONDS
            )
            if not acquired:
                raise JobAlreadyRunning("loader already has an unexpired job lease")
        warehouse.require_job_lock(owner, remaining_seconds=timeout_seconds)
        run_dbt(
            target=target,
            timeout_seconds=timeout_seconds,
            selector=selector,
        )
        warehouse.require_job_lock(owner)
        return 0
    finally:
        if acquired and warehouse.connection_usable:
            warehouse.release_job_lock("loader", owner)
        warehouse.close()
