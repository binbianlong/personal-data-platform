from __future__ import annotations

import os
import plistlib
import stat
import sys
from pathlib import Path

import pytest

from personal_data_platform.config import ConfigurationError
from personal_data_platform.entrypoint import main
from personal_data_platform.launchd import (
    LAUNCH_AGENT_LABEL,
    LaunchAgentSettings,
    build_launch_agent,
    write_launch_agent,
)


def _environment() -> dict[str, str]:
    return {
        "B2_APPLICATION_KEY": "must-not-be-serialized",
        "B2_BUCKET": "screen-time-raw",
        "B2_ENDPOINT": "https://s3.us-west-004.backblazeb2.com",
        "B2_KEY_ID": "must-not-be-serialized",
        "B2_REGION": "us-west-004",
        "PDP_COLLECTOR_POLL_SECONDS": "60",
        "PDP_PSEUDONYM_KEY_HEX": "42" * 32,
        "PDP_SCREEN_TIME_DEVICE_ALLOWLIST": f"{'b' * 64},{'a' * 64}",
    }


def _settings(tmp_path, *, environ: dict[str, str] | None = None) -> LaunchAgentSettings:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='test'\n")
    return LaunchAgentSettings.from_env(
        project_root=project_root,
        python_executable=Path(sys.executable),
        log_directory=tmp_path / "logs",
        environ=environ or _environment(),
    )


def test_builds_secret_free_keepalive_launch_agent(tmp_path) -> None:
    settings = _settings(tmp_path)

    decoded = plistlib.loads(build_launch_agent(settings))

    assert decoded["Label"] == LAUNCH_AGENT_LABEL
    assert decoded["RunAtLoad"] is True
    assert decoded["KeepAlive"] is True
    assert decoded["Umask"] == 0o077
    assert decoded["ProgramArguments"] == [
        str(settings.python_executable),
        "-m",
        "personal_data_platform.entrypoint",
        "screen-time",
        "collect",
        "--watch",
    ]
    environment = decoded["EnvironmentVariables"]
    assert environment["PDP_SCREEN_TIME_DEVICE_ALLOWLIST"] == f"{'a' * 64},{'b' * 64}"
    assert environment["PDP_COLLECTOR_POLL_SECONDS"] == "60"
    assert "PDP_PSEUDONYM_KEY_HEX" not in environment
    assert "B2_KEY_ID" not in environment
    assert "B2_APPLICATION_KEY" not in environment
    assert b"must-not-be-serialized" not in build_launch_agent(settings)


def test_writes_private_plist_and_log_directory(tmp_path) -> None:
    settings = _settings(tmp_path)
    destination = tmp_path / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

    assert write_launch_agent(settings, destination) == destination.resolve()

    assert plistlib.loads(destination.read_bytes())["Label"] == LAUNCH_AGENT_LABEL
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.log_directory.stat().st_mode) == 0o700
    assert not list(destination.parent.glob(f".{destination.name}.*"))


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("B2_ENDPOINT", "http://example.invalid", "credential-free HTTPS URL"),
        ("PDP_SCREEN_TIME_DEVICE_ALLOWLIST", "raw-device-id", "HMAC-SHA-256"),
        ("PDP_COLLECTOR_POLL_SECONDS", "nan", "finite value"),
    ],
)
def test_rejects_unsafe_launch_agent_configuration(
    tmp_path,
    name: str,
    value: str,
    message: str,
) -> None:
    environment = _environment()
    environment[name] = value

    with pytest.raises(ConfigurationError, match=message):
        _settings(tmp_path, environ=environment)


def test_python_executable_must_be_runnable(tmp_path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='test'\n")
    not_executable = tmp_path / "python"
    not_executable.write_text("not executable")
    os.chmod(not_executable, 0o600)

    with pytest.raises(ConfigurationError, match="not runnable"):
        LaunchAgentSettings.from_env(
            project_root=project_root,
            python_executable=not_executable,
            environ=_environment(),
        )


def test_launch_agent_cli_writes_unloaded_plist(tmp_path, monkeypatch, capsys) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='test'\n")
    destination = tmp_path / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    log_directory = tmp_path / "logs"
    for name, value in _environment().items():
        monkeypatch.setenv(name, value)

    assert (
        main(
            [
                "screen-time",
                "launch-agent",
                "--output",
                str(destination),
                "--project-root",
                str(project_root),
                "--log-directory",
                str(log_directory),
            ]
        )
        == 0
    )

    assert capsys.readouterr().out.strip() == str(destination.resolve())
    assert plistlib.loads(destination.read_bytes())["RunAtLoad"] is True
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
