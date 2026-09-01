"""Runtime configuration loaded without storing secrets in project files."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

KEYCHAIN_SERVICE = "personal-data-platform"
DEFAULT_COLLECTOR_ADC_PATH = (
    Path.home()
    / "Library/Application Support/personal-data-platform/gcloud/application_default_credentials.json"
)
DEFAULT_REBUILD_ADC_PATH = (
    Path.home()
    / "Library/Application Support/personal-data-platform/gcloud-rebuild/application_default_credentials.json"
)


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is absent or invalid."""


@dataclass(frozen=True, slots=True)
class GCSConfig:
    """Google Cloud Storage project and production Raw bucket settings."""

    project_id: str
    bucket: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> GCSConfig:
        values = os.environ if environ is None else environ
        return cls(
            project_id=_required(values, "GOOGLE_CLOUD_PROJECT"),
            bucket=_required(values, "GCS_BUCKET"),
        )


@dataclass(frozen=True, slots=True)
class CollectorADCConfig:
    """Explicit impersonated ADC used by the unattended local collector."""

    credentials_path: Path
    service_account_email: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CollectorADCConfig:
        values = os.environ if environ is None else environ
        credentials_path = Path(
            values.get("GOOGLE_APPLICATION_CREDENTIALS", str(DEFAULT_COLLECTOR_ADC_PATH))
        ).expanduser()
        service_account_email = _required(values, "PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL")
        _validate_impersonated_adc(
            credentials_path,
            service_account_email,
            credentials_name="GOOGLE_APPLICATION_CREDENTIALS",
            service_account_name="PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL",
        )
        return cls(
            credentials_path=credentials_path.resolve(),
            service_account_email=service_account_email,
        )


@dataclass(frozen=True, slots=True)
class RebuildADCConfig:
    """Explicit read-only impersonated ADC used by local rebuild commands."""

    credentials_path: Path
    service_account_email: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RebuildADCConfig:
        values = os.environ if environ is None else environ
        credentials_path = Path(
            values.get("PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS", str(DEFAULT_REBUILD_ADC_PATH))
        ).expanduser()
        service_account_email = _required(values, "PDP_REBUILD_SERVICE_ACCOUNT_EMAIL")
        _validate_impersonated_adc(
            credentials_path,
            service_account_email,
            credentials_name="PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS",
            service_account_name="PDP_REBUILD_SERVICE_ACCOUNT_EMAIL",
        )
        return cls(
            credentials_path=credentials_path.resolve(),
            service_account_email=service_account_email,
        )


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Local Screen Time collector settings."""

    sync_db_path: Path
    app_in_focus_remote_dir: Path
    state_db_path: Path
    pseudonym_key: bytes
    device_allowlist: frozenset[str]
    gcs: GCSConfig | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_gcs: bool = True,
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
            gcs=GCSConfig.from_env(values) if require_gcs else None,
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


def _validate_impersonated_adc(
    path: Path,
    expected_service_account: str,
    *,
    credentials_name: str,
    service_account_name: str,
) -> None:
    if not path.is_file():
        raise ConfigurationError(f"{credentials_name} is not a file: {path}")
    metadata = path.stat()
    if metadata.st_uid != os.getuid():
        raise ConfigurationError(f"{credentials_name} must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ConfigurationError(f"{credentials_name} must have mode 0600")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"{credentials_name} must contain valid JSON") from error
    if not isinstance(decoded, dict) or decoded.get("type") != "impersonated_service_account":
        raise ConfigurationError(f"{credentials_name} must be impersonated service-account ADC")
    source = decoded.get("source_credentials")
    if not isinstance(source, dict) or source.get("type") != "authorized_user":
        raise ConfigurationError(
            "impersonated ADC must use user ADC instead of a service-account key"
        )
    impersonation_url = decoded.get("service_account_impersonation_url")
    if not isinstance(impersonation_url, str):
        raise ConfigurationError(
            "impersonated ADC is missing its service-account impersonation URL"
        )
    parsed = urlsplit(impersonation_url)
    prefix = "/v1/projects/-/serviceAccounts/"
    suffix = ":generateAccessToken"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "iamcredentials.googleapis.com"
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(prefix)
        or not parsed.path.endswith(suffix)
    ):
        raise ConfigurationError(
            "impersonated ADC has an invalid service-account impersonation URL"
        )
    actual_service_account = unquote(parsed.path[len(prefix) : -len(suffix)])
    if actual_service_account != expected_service_account:
        raise ConfigurationError(f"ADC impersonation target does not match {service_account_name}")
    if (
        re.fullmatch(
            r"[a-z0-9][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9.-]+\.iam\.gserviceaccount\.com",
            expected_service_account,
        )
        is None
    ):
        raise ConfigurationError(f"{service_account_name} is invalid")
