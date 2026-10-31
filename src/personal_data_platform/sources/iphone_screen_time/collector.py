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

from .raw import APP_IN_FOCUS_STREAM


class BiomeScreenTimeSource:
    """Read-only access to iPhone device and App.InFocus segment locations."""

    def __init__(self, *, sync_db_path: Path, remote_dir: Path) -> None:
        self.sync_db_path = sync_db_path
        self.remote_dir = remote_dir

    raw_stream = APP_IN_FOCUS_STREAM
    segment_stream = "App.InFocus"
    platform = "ios"

    def list_devices(self) -> list[BiomeDevice]:
        return self.list_iphone_devices()

    def list_iphone_devices(self) -> list[BiomeDevice]:
        if not self.sync_db_path.is_file():
            raise CollectorSourceError(f"Biome sync database is not readable: {self.sync_db_path}")
        uri = f"file:{quote(str(self.sync_db_path), safe='/')}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                rows = connection.execute(
                    """
                    SELECT device_identifier, name, model
                    FROM DevicePeer
                    WHERE platform = 2
                    ORDER BY device_identifier
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise CollectorSourceError(f"failed to read Biome DevicePeer: {error}") from error
        devices: list[BiomeDevice] = []
        for identifier, name, model in rows:
            _validate_device_identifier(identifier)
            devices.append(BiomeDevice(identifier=identifier, name=name, model=model))
        return devices

    def device_directory(self, device: BiomeDevice) -> Path:
        _validate_device_identifier(device.identifier)
        return self.remote_dir / device.identifier

    def list_segments(self, device: BiomeDevice) -> list[tuple[Path, str]]:
        directory = self.device_directory(device)
        return list_segment_files(directory)
