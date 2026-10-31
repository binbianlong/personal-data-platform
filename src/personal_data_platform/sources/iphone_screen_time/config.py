"""Independent local collector settings and credentials."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from personal_data_platform.biome.keychain import _env_or_keychain
from personal_data_platform.config import ConfigurationError

ENV_PREFIX = "PDP_IPHONE_SCREEN_TIME_"


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    sync_db_path: Path
    app_in_focus_remote_dir: Path
    state_db_path: Path
    pseudonym_key: bytes
    device_allowlist: frozenset[str]
    poll_seconds: float = 1800

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, require_allowlist: bool = True
    ) -> CollectorConfig:
        values = os.environ if environ is None else environ
        library = Path.home() / "Library"
        key_hex = _env_or_keychain(
            values, ENV_PREFIX + "PSEUDONYM_KEY_HEX", "iphone-screen-time-pseudonym-key-hex"
        )
        try:
            pseudonym_key = bytes.fromhex(key_hex)
        except ValueError as error:
            raise ConfigurationError(
                ENV_PREFIX + "PSEUDONYM_KEY_HEX must be valid hexadecimal"
            ) from error
        if len(pseudonym_key) < 32:
            raise ConfigurationError("the pseudonym key must be at least 32 bytes")
        devices = frozenset(
            v.strip()
            for v in values.get(ENV_PREFIX + "DEVICE_ALLOWLIST", "").split(",")
            if v.strip()
        )
        if any(len(v) != 64 or any(c not in "0123456789abcdef" for c in v) for v in devices):
            raise ConfigurationError(
                ENV_PREFIX + "DEVICE_ALLOWLIST must contain lowercase HMAC-SHA-256 keys"
            )
        if require_allowlist and not devices:
            raise ConfigurationError(ENV_PREFIX + "DEVICE_ALLOWLIST is required")
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
            app_in_focus_remote_dir=Path(
                values.get(
                    ENV_PREFIX + "APP_IN_FOCUS_REMOTE_DIR",
                    library / "Biome/streams/restricted/App.InFocus/remote",
                )
            ).expanduser(),
            state_db_path=Path(
                values.get(
                    ENV_PREFIX + "STATE_DB_PATH",
                    library
                    / "Application Support/personal-data-platform/iphone_screen_time/collector.db",
                )
            ).expanduser(),
            pseudonym_key=pseudonym_key,
            device_allowlist=devices,
            poll_seconds=poll_seconds,
        )
