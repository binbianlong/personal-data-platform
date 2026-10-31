"""Independent source collection and warehouse loading entry points."""

from __future__ import annotations

import os
from datetime import datetime

from personal_data_platform.biome import runtime as _runtime
from personal_data_platform.biome.collector import ScreenTimeCollector
from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.biome.keychain import _env_or_keychain
from personal_data_platform.config import ConfigurationError
from personal_data_platform.storage.factory import WarehouseConfig
from personal_data_platform.storage.warehouse import Warehouse

from .adapter import IPhoneScreenTimeSource
from .collector import BiomeScreenTimeSource
from .config import CollectorConfig
from .raw import SOURCE_ID


def collect(*, watch: bool) -> int:
    config = CollectorConfig.from_env()
    state = CollectorState(config.state_db_path, source_id=SOURCE_ID)
    collector = ScreenTimeCollector(
        source=collector_source(config),
        state=state,
        pseudonym_key=config.pseudonym_key,
        allowed_device_keys=config.device_allowlist,
    )
    return _runtime.collect(
        collector,
        state,
        source=IPhoneScreenTimeSource(),
        warehouse_config=warehouse_config,
        poll_seconds=config.poll_seconds,
        watch=watch,
    )


def warehouse_config() -> WarehouseConfig:
    database = os.environ.get("MOTHERDUCK_DATABASE", "").strip()
    if not database:
        raise ConfigurationError("MOTHERDUCK_DATABASE is required")
    token = _env_or_keychain(
        os.environ, "PDP_IPHONE_SCREEN_TIME_MOTHERDUCK_TOKEN", "iphone-screen-time-motherduck-token"
    )
    return WarehouseConfig(database=database, token=token)


def load_pending(
    state: CollectorState,
    warehouse: Warehouse,
    *,
    now: datetime | None = None,
    publish_success: bool = True,
) -> None:
    _runtime.load_pending(
        state, warehouse, source=IPhoneScreenTimeSource(), now=now, publish_success=publish_success
    )


def collector_source(config: CollectorConfig) -> BiomeScreenTimeSource:
    return BiomeScreenTimeSource(
        sync_db_path=config.sync_db_path, remote_dir=config.app_in_focus_remote_dir
    )
