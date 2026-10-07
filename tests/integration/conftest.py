"""Shared fixtures for warehouse and analytics integration tests."""

import shutil
from pathlib import Path

import pytest

from personal_data_platform.dbt_runner import DBT_PROJECT_DIR


@pytest.fixture
def dbt_project(tmp_path: Path) -> Path:
    """Give each dbt run its own project, logs, and generated artifacts."""
    project = tmp_path / "dbt"
    project.mkdir()
    for filename in ("dbt_project.yml", "profiles.yml"):
        shutil.copyfile(DBT_PROJECT_DIR / filename, project / filename)
    for directory in ("models", "macros", "tests"):
        shutil.copytree(DBT_PROJECT_DIR / directory, project / directory)
    return project
