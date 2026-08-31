from pathlib import Path

from personal_data_platform.storage.motherduck import DEFAULT_MIGRATIONS

DOCKERFILE = Path(__file__).parents[2] / "Dockerfile"


def test_dockerfile_installs_the_project() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert dockerfile.startswith("FROM python:3.13-slim\n")
    assert "COPY pyproject.toml /app/" in dockerfile
    assert "COPY src /app/src" in dockerfile
    assert "COPY dbt /app/dbt" in dockerfile
    assert "COPY sql" not in dockerfile
    assert "RUN python -m pip install --no-cache-dir ." in dockerfile
    assert "PDP_PROJECT_ROOT=/app" in dockerfile


def test_dockerfile_uses_the_shared_entrypoint() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert 'ENTRYPOINT ["python", "-m", "personal_data_platform.entrypoint"]' in dockerfile


def test_dockerfile_includes_the_dbt_project_at_the_configured_root() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "COPY dbt /app/dbt" in dockerfile
    assert "PDP_PROJECT_ROOT=/app" in dockerfile


def test_default_warehouse_migrations_are_packaged_with_the_application() -> None:
    assert DEFAULT_MIGRATIONS.name == "migrations"
    assert (DEFAULT_MIGRATIONS / "001_initial.sql").is_file()


def test_dockerfile_drops_root_before_runtime() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "useradd --uid 10001" in dockerfile
    assert "USER 10001" in dockerfile
