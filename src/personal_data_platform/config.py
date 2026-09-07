"""Runtime configuration loaded without storing secrets in project files."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

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


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
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
