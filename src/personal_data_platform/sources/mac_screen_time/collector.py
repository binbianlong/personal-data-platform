"""Read-only source discovery for Biome segments."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from personal_data_platform.biome.collector import (
    BiomeDevice,
    CollectorSourceError,
    _validate_device_identifier,
    list_segment_files,
)

from .raw import APP_USAGE_STREAM


class BiomeMacAppUsageSource:
    """Read the single local Mac DevicePeer and its AppUsage stream."""

    raw_stream = APP_USAGE_STREAM
    segment_stream = "ScreenTime.AppUsage"
    platform = "macos"

    def __init__(self, *, sync_db_path: Path, local_dir: Path) -> None:
        self.sync_db_path = sync_db_path
        self.local_dir = local_dir

    def list_devices(self) -> list[BiomeDevice]:
        if not self.sync_db_path.is_file():
            raise CollectorSourceError(f"Biome sync database is not readable: {self.sync_db_path}")
        uri = f"file:{quote(str(self.sync_db_path), safe='/')}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                rows = connection.execute(
                    """SELECT device_identifier, name, model FROM DevicePeer
                    WHERE platform = 3 AND me = 1"""
                ).fetchall()
        except sqlite3.Error as error:
            raise CollectorSourceError(f"failed to read local Mac DevicePeer: {error}") from error
        if len(rows) != 1:
            raise CollectorSourceError("expected exactly one platform=3 me=1 Mac in Biome sync.db")
        identifier, name, model = rows[0]
        _validate_device_identifier(identifier)
        return [BiomeDevice(identifier=identifier, name=name, model=model)]

    def device_directory(self, device: BiomeDevice) -> Path:
        _validate_device_identifier(device.identifier)
        return self.local_dir

    def list_segments(self, device: BiomeDevice) -> list[tuple[Path, str]]:
        return list_segment_files(self.device_directory(device))
