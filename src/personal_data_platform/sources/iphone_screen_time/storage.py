"""Read this source's durable local Raw archive."""

from __future__ import annotations

import os
from pathlib import Path

from personal_data_platform.biome.collector_state import CollectorState
from personal_data_platform.biome.storage import BiomeLocalRepository
from personal_data_platform.sources.contracts import RawCodec

from .raw import SOURCE_ID


class LocalRepository(BiomeLocalRepository):
    @classmethod
    def from_env(cls, *, source: RawCodec) -> LocalRepository:
        state_path = Path(
            os.environ.get(
                "PDP_IPHONE_SCREEN_TIME_STATE_DB_PATH",
                str(
                    Path.home()
                    / "Library/Application Support/personal-data-platform/iphone_screen_time/collector.db"
                ),
            )
        ).expanduser()
        if not state_path.is_file():
            raise ValueError(f"Raw SQLite does not exist: {state_path}")
        return cls(state=CollectorState(state_path, source_id=SOURCE_ID), source=source)
