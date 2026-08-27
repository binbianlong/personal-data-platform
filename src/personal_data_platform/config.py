"""Runtime configuration loaded without storing secrets in project files."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

KEYCHAIN_SERVICE = "personal-data-platform"


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is absent or invalid."""


@dataclass(frozen=True, slots=True)
class B2Config:
    """Backblaze B2 S3-compatible connection settings."""

    endpoint: str
    key_id: str
    application_key: str
    bucket: str
    region: str = "us-west-004"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> B2Config:
        values = os.environ if environ is None else environ
        return cls(
            endpoint=_required(values, "B2_ENDPOINT"),
            key_id=_env_or_keychain(values, "B2_KEY_ID", "b2-key-id"),
            application_key=_env_or_keychain(values, "B2_APPLICATION_KEY", "b2-application-key"),
            bucket=_required(values, "B2_BUCKET"),
            region=values.get("B2_REGION", "us-west-004"),
        )


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Local Screen Time collector settings."""

    sync_db_path: Path
    app_in_focus_remote_dir: Path
    state_db_path: Path
    pseudonym_key: bytes
    device_allowlist: frozenset[str]
    b2: B2Config | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_b2: bool = True,
        require_allowlist: bool = True,
    ) -> CollectorConfig:
        values = os.environ if environ is None else environ
        library = Path.home() / "Library"
        key_hex = _env_or_keychain(
            values,
            "PDP_PSEUDONYM_KEY_HEX",
            "screen-time-pseudonym-key-hex",
        )
        try:
            pseudonym_key = bytes.fromhex(key_hex)
        except ValueError as error:
            raise ConfigurationError("PDP_PSEUDONYM_KEY_HEX must be valid hexadecimal") from error
        if len(pseudonym_key) < 32:
            raise ConfigurationError("the Screen Time pseudonym key must be at least 32 bytes")

        allowlist = frozenset(
            value.strip()
            for value in values.get("PDP_SCREEN_TIME_DEVICE_ALLOWLIST", "").split(",")
            if value.strip()
        )
        invalid_keys = sorted(
            key
            for key in allowlist
            if len(key) != 64 or any(character not in "0123456789abcdef" for character in key)
        )
        if invalid_keys:
            raise ConfigurationError(
                "PDP_SCREEN_TIME_DEVICE_ALLOWLIST must contain lowercase HMAC-SHA-256 keys"
            )
        if require_allowlist and not allowlist:
            raise ConfigurationError("PDP_SCREEN_TIME_DEVICE_ALLOWLIST is required")

        return cls(
            sync_db_path=Path(
                values.get("PDP_SYNC_DB_PATH", library / "Biome/sync/sync.db")
            ).expanduser(),
            app_in_focus_remote_dir=Path(
                values.get(
                    "PDP_APP_IN_FOCUS_REMOTE_DIR",
                    library / "Biome/streams/restricted/App.InFocus/remote",
                )
            ).expanduser(),
            state_db_path=Path(
                values.get(
                    "PDP_COLLECTOR_STATE_DB_PATH",
                    library / "Application Support/personal-data-platform/collector.db",
                )
            ).expanduser(),
            pseudonym_key=pseudonym_key,
            device_allowlist=allowlist,
            b2=B2Config.from_env(values) if require_b2 else None,
        )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _env_or_keychain(environ: Mapping[str, str], env_name: str, account: str) -> str:
    value = environ.get(env_name, "").strip()
    if value:
        return value
    return _read_keychain(account, env_name)


def _read_keychain(account: str, env_name: str) -> str:
    try:
        completed = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                account,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"{env_name} is required (macOS Keychain command is unavailable)"
        ) from error
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise ConfigurationError(
            f"{env_name} is required or Keychain account {account!r} must exist"
        )
    return value
