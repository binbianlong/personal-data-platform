"""Local iPhone Screen Time collection settings and credentials."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from personal_data_platform.config import (
    ConfigurationError,
    GCSConfig,
    _required,
    _validate_impersonated_adc,
)

KEYCHAIN_SERVICE = "personal-data-platform"
DEFAULT_COLLECTOR_ADC_PATH = (
    Path.home()
    / "Library/Application Support/personal-data-platform/gcloud/application_default_credentials.json"
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


def _env_or_keychain(environ: Mapping[str, str], env_name: str, account: str) -> str:
    value = environ.get(env_name, "").strip()
    if value:
        return value
    return _read_keychain(account, env_name)


def _read_keychain(account: str, env_name: str) -> str:
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    except OSError as error:
        raise ConfigurationError(
            f"{env_name} is required (macOS Keychain is unavailable)"
        ) from error

    find_password = security.SecKeychainFindGenericPassword
    find_password.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    find_password.restype = ctypes.c_int32
    free_content = security.SecKeychainItemFreeContent
    free_content.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    free_content.restype = ctypes.c_int32

    service_bytes = KEYCHAIN_SERVICE.encode("utf-8")
    account_bytes = account.encode("utf-8")
    password_length = ctypes.c_uint32()
    password_data = ctypes.c_void_p()
    try:
        status = find_password(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            None,
        )
        if status != 0 or not password_data.value or password_length.value == 0:
            raise ConfigurationError(
                f"{env_name} is required or Keychain account {account!r} must exist"
            )
        try:
            value = ctypes.string_at(password_data, password_length.value).decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise ConfigurationError(f"Keychain account {account!r} must contain UTF-8") from error
        if not value:
            raise ConfigurationError(f"Keychain account {account!r} is empty")
        return value
    finally:
        if password_data.value:
            free_content(None, password_data)
