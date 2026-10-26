from pathlib import Path

DOCKERFILE = Path(__file__).parents[2] / "Dockerfile"


def test_dockerfile_drops_root_before_runtime() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "useradd --uid 10001" in dockerfile
    assert "USER 10001" in dockerfile
