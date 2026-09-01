from __future__ import annotations

import json
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


def _environment(tmp_path: Path) -> dict[str, str]:
    service_account = "collector@synthetic-project.iam.gserviceaccount.com"
    adc_path = tmp_path / "gcloud/application_default_credentials.json"
    adc_path.parent.mkdir(parents=True, exist_ok=True)
    adc_path.write_text(
        json.dumps(
            {
                "type": "impersonated_service_account",
                "service_account_impersonation_url": (
                    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                    f"{service_account}:generateAccessToken"
                ),
                "source_credentials": {"type": "authorized_user"},
            }
        )
    )
    os.chmod(adc_path, 0o600)
    return {
        "GCS_BUCKET": "screen-time-raw",
        "GOOGLE_APPLICATION_CREDENTIALS": str(adc_path),
        "GOOGLE_CLOUD_PROJECT": "synthetic-project",
        "PDP_COLLECTOR_POLL_SECONDS": "60",
        "PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL": service_account,
        "PDP_COLLECTOR_STATE_DB_PATH": str(tmp_path / "state/collector.db"),
        "PDP_PSEUDONYM_KEY_HEX": "42" * 32,
        "PDP_APP_IN_FOCUS_REMOTE_DIR": str(tmp_path / "Biome/App.InFocus/remote"),
        "PDP_SCREEN_TIME_DEVICE_ALLOWLIST": f"{'b' * 64},{'a' * 64}",
        "PDP_SYNC_DB_PATH": str(tmp_path / "Biome/sync.db"),
    }


def _settings(tmp_path, *, environ: dict[str, str] | None = None) -> LaunchAgentSettings:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='test'\n")
    return LaunchAgentSettings.from_env(
        project_root=project_root,
        python_executable=Path(sys.executable),
        log_directory=tmp_path / "logs",
        environ=_environment(tmp_path) if environ is None else environ,
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
    assert environment["GOOGLE_CLOUD_PROJECT"] == "synthetic-project"
    assert environment["GCS_BUCKET"] == "screen-time-raw"
    assert environment["GOOGLE_APPLICATION_CREDENTIALS"] == str(
        settings.google_application_credentials
    )
    assert environment["PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL"] == (
        "collector@synthetic-project.iam.gserviceaccount.com"
    )
    assert "PDP_PSEUDONYM_KEY_HEX" not in environment


def test_preserves_collector_paths_in_launch_agent(tmp_path) -> None:
    configured = _environment(tmp_path)
    settings = _settings(tmp_path, environ=configured)

    environment = plistlib.loads(build_launch_agent(settings))["EnvironmentVariables"]

    assert environment["PDP_SYNC_DB_PATH"] == configured["PDP_SYNC_DB_PATH"]
    assert environment["PDP_APP_IN_FOCUS_REMOTE_DIR"] == configured["PDP_APP_IN_FOCUS_REMOTE_DIR"]
    assert environment["PDP_COLLECTOR_STATE_DB_PATH"] == configured["PDP_COLLECTOR_STATE_DB_PATH"]


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
        (
            "PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL",
            "other@synthetic-project.iam.gserviceaccount.com",
            "target does not match",
        ),
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
    environment = _environment(tmp_path)
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
            environ=_environment(tmp_path),
        )


def test_collector_adc_file_must_be_private(tmp_path) -> None:
    environment = _environment(tmp_path)
    os.chmod(environment["GOOGLE_APPLICATION_CREDENTIALS"], 0o644)

    with pytest.raises(ConfigurationError, match="mode 0600"):
        _settings(tmp_path, environ=environment)


def test_python_executable_preserves_virtual_environment_path(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='test'\n")
    virtualenv_python = tmp_path / "venv/bin/python"
    virtualenv_python.parent.mkdir(parents=True)
    virtualenv_python.symlink_to(sys.executable)
    monkeypatch.setattr(
        "personal_data_platform.launchd._validate_python_runtime",
        lambda *_: None,
    )

    settings = LaunchAgentSettings.from_env(
        project_root=project_root,
        python_executable=virtualenv_python,
        environ=_environment(tmp_path),
    )

    assert settings.python_executable == virtualenv_python.absolute()


def test_python_executable_must_import_collector_entrypoint(tmp_path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='test'\n")

    with pytest.raises(ConfigurationError, match="cannot import"):
        LaunchAgentSettings.from_env(
            project_root=project_root,
            python_executable=Path("/bin/sh"),
            environ=_environment(tmp_path),
        )


def test_launch_agent_cli_writes_unloaded_plist(tmp_path, monkeypatch, capsys) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='test'\n")
    destination = tmp_path / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    log_directory = tmp_path / "logs"
    for name, value in _environment(tmp_path).items():
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
