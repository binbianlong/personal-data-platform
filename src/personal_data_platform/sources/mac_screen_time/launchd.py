"""Generate a secret-free macOS LaunchAgent for the Screen Time collector."""

from __future__ import annotations

import math
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from personal_data_platform.config import (
    ConfigurationError,
)

LAUNCH_AGENT_LABEL = "com.personal-data-platform.mac-screen-time-collector"
_DEVICE_KEY = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True, slots=True)
class LaunchAgentSettings:
    """Non-secret values embedded in the per-user LaunchAgent plist."""

    python_executable: Path
    project_root: Path
    log_directory: Path
    sync_db_path: Path
    app_usage_local_dir: Path
    state_db_path: Path
    motherduck_database: str
    device_key: str
    poll_seconds: str

    @classmethod
    def from_env(
        cls,
        *,
        project_root: Path,
        python_executable: Path | None = None,
        log_directory: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> LaunchAgentSettings:
        values = os.environ if environ is None else environ
        executable = (python_executable or Path(sys.executable)).expanduser().absolute()
        root = project_root.expanduser().resolve()
        library = Path.home() / "Library"
        logs = (
            (log_directory or Path.home() / "Library/Logs/personal-data-platform")
            .expanduser()
            .resolve()
        )

        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ConfigurationError(f"Python executable is not runnable: {executable}")
        if not (root / "pyproject.toml").is_file():
            raise ConfigurationError(f"project root does not contain pyproject.toml: {root}")
        _validate_python_runtime(executable, root)

        database = _required(values, "MOTHERDUCK_DATABASE")

        devices = values.get("PDP_MAC_SCREEN_TIME_DEVICE_KEY", "").strip()
        if devices and _DEVICE_KEY.fullmatch(devices) is None:
            raise ConfigurationError(
                "PDP_MAC_SCREEN_TIME_DEVICE_KEY must be lowercase HMAC-SHA-256"
            )
        if not devices:
            raise ConfigurationError("PDP_MAC_SCREEN_TIME_DEVICE_KEY is required")

        poll_seconds = values.get("PDP_MAC_SCREEN_TIME_POLL_SECONDS", "1800").strip()
        try:
            parsed_poll_seconds = float(poll_seconds)
        except ValueError as error:
            raise ConfigurationError("PDP_MAC_SCREEN_TIME_POLL_SECONDS must be numeric") from error
        if not math.isfinite(parsed_poll_seconds) or parsed_poll_seconds < 10:
            raise ConfigurationError(
                "PDP_MAC_SCREEN_TIME_POLL_SECONDS must be a finite value of at least 10 seconds"
            )

        return cls(
            python_executable=executable,
            project_root=root,
            log_directory=logs,
            sync_db_path=_path_from_env(
                values,
                "PDP_MAC_SCREEN_TIME_SYNC_DB_PATH",
                library / "Biome/sync/sync.db",
            ),
            app_usage_local_dir=_path_from_env(
                values,
                "PDP_MAC_SCREEN_TIME_APP_USAGE_LOCAL_DIR",
                library / "Biome/streams/restricted/ScreenTime.AppUsage/local",
            ),
            state_db_path=_path_from_env(
                values,
                "PDP_MAC_SCREEN_TIME_STATE_DB_PATH",
                library / "Application Support/personal-data-platform/mac_screen_time/collector.db",
            ),
            motherduck_database=database,
            device_key=devices,
            poll_seconds=poll_seconds,
        )


def build_launch_agent(settings: LaunchAgentSettings) -> bytes:
    """Serialize the collector LaunchAgent without embedding Keychain secrets."""

    environment = {
        "MOTHERDUCK_DATABASE": settings.motherduck_database,
        "PATH": _DEFAULT_PATH,
        "PDP_MAC_SCREEN_TIME_APP_USAGE_LOCAL_DIR": str(settings.app_usage_local_dir),
        "PDP_MAC_SCREEN_TIME_POLL_SECONDS": settings.poll_seconds,
        "PDP_MAC_SCREEN_TIME_STATE_DB_PATH": str(settings.state_db_path),
        "PDP_MAC_SCREEN_TIME_DEVICE_KEY": settings.device_key,
        "PDP_MAC_SCREEN_TIME_SYNC_DB_PATH": str(settings.sync_db_path),
        "PYTHONUNBUFFERED": "1",
    }

    payload = {
        "EnvironmentVariables": environment,
        "KeepAlive": True,
        "Label": LAUNCH_AGENT_LABEL,
        "ProcessType": "Background",
        "ProgramArguments": [
            str(settings.python_executable),
            "-m",
            "personal_data_platform.entrypoint",
            "mac-screen-time",
            "collect",
            "--watch",
        ],
        "RunAtLoad": True,
        "StandardErrorPath": str(settings.log_directory / "mac-screen-time-collector.stderr.log"),
        "StandardOutPath": str(settings.log_directory / "mac-screen-time-collector.stdout.log"),
        "ThrottleInterval": 30,
        "Umask": 0o077,
        "WorkingDirectory": str(settings.project_root),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def write_launch_agent(settings: LaunchAgentSettings, output_path: Path) -> Path:
    """Atomically write a mode-0600 plist and create its private log directory."""

    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    settings.log_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.log_directory, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as temporary:
            descriptor = -1
            temporary.write(build_launch_agent(settings))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
    return destination


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _path_from_env(environ: Mapping[str, str], name: str, default: Path) -> Path:
    value = environ.get(name)
    path = Path(value) if value is not None else default
    return path.expanduser().resolve()


def _validate_python_runtime(executable: Path, project_root: Path) -> None:
    try:
        subprocess.run(
            [
                str(executable),
                "-I",
                "-c",
                "import personal_data_platform.entrypoint",
            ],
            cwd=project_root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ConfigurationError(
            f"Python executable cannot import personal_data_platform.entrypoint: {executable}"
        ) from error
