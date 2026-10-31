"""Independent local collector settings and credentials."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from personal_data_platform.biome.keychain import _env_or_keychain
from personal_data_platform.config import ConfigurationError

ENV_PREFIX = "PDP_MAC_SCREEN_TIME_"


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    sync_db_path: Path
    app_usage_local_dir: Path
    state_db_path: Path
    pseudonym_key: bytes
    device_key: str | None
    poll_seconds: float = 1800

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, require_allowlist: bool = True
    ) -> CollectorConfig:
        values = os.environ if environ is None else environ
        library = Path.home() / "Library"
        key_hex = _env_or_keychain(
            values, ENV_PREFIX + "PSEUDONYM_KEY_HEX", "mac-screen-time-pseudonym-key-hex"
        )
        try:
            pseudonym_key = bytes.fromhex(key_hex)
        except ValueError as error:
            raise ConfigurationError(
                ENV_PREFIX + "PSEUDONYM_KEY_HEX must be valid hexadecimal"
            ) from error
        if len(pseudonym_key) < 32:
            raise ConfigurationError("the pseudonym key must be at least 32 bytes")
        devices = values.get(ENV_PREFIX + "DEVICE_KEY", "").strip() or None
        if devices is not None and (
            len(devices) != 64 or any(c not in "0123456789abcdef" for c in devices)
        ):
            raise ConfigurationError(ENV_PREFIX + "DEVICE_KEY must be a lowercase HMAC-SHA-256 key")
        if require_allowlist and not devices:
            raise ConfigurationError(ENV_PREFIX + "DEVICE_KEY is required")
        try:
            poll_seconds = float(values.get(ENV_PREFIX + "POLL_SECONDS", "1800"))
        except ValueError as error:
            raise ConfigurationError(ENV_PREFIX + "POLL_SECONDS must be numeric") from error
        if not math.isfinite(poll_seconds) or poll_seconds < 10:
            raise ConfigurationError(
                ENV_PREFIX + "POLL_SECONDS must be a finite value of at least 10 seconds"
            )
        return cls(
            sync_db_path=Path(
                values.get(ENV_PREFIX + "SYNC_DB_PATH", library / "Biome/sync/sync.db")
            ).expanduser(),
            app_usage_local_dir=Path(
                values.get(
                    ENV_PREFIX + "APP_USAGE_LOCAL_DIR",
                    library / "Biome/streams/restricted/ScreenTime.AppUsage/local",
                )
            ).expanduser(),
            state_db_path=Path(
                values.get(
                    ENV_PREFIX + "STATE_DB_PATH",
                    library
                    / "Application Support/personal-data-platform/mac_screen_time/collector.db",
                )
            ).expanduser(),
            pseudonym_key=pseudonym_key,
            device_key=devices,
            poll_seconds=poll_seconds,
        )
