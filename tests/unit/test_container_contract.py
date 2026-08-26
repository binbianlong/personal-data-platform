from pathlib import Path

DOCKERFILE = Path(__file__).parents[2] / "Dockerfile"


def test_dockerfile_installs_the_project() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert dockerfile.startswith("FROM python:3.13-slim\n")
    assert "COPY pyproject.toml /app/" in dockerfile
    assert "COPY src /app/src" in dockerfile
    assert "RUN python -m pip install --no-cache-dir ." in dockerfile


def test_dockerfile_uses_the_shared_entrypoint() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert 'ENTRYPOINT ["python", "-m", "personal_data_platform.entrypoint"]' in dockerfile
