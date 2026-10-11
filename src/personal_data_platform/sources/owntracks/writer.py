"""Write locations inside the common warehouse-owned Raw transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from personal_data_platform.raw.models import RawObject

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


@dataclass(frozen=True)
class OwnTracksBatch:
    message: dict[str, object]
    parser_version: str

    @property
    def record_count(self) -> int:
        return 1

    def write(
        self, connection: DuckDBPyConnection, raw: RawObject, *, byte_size: int, loaded_at: datetime
    ) -> None:
        connection.execute(
            """INSERT OR REPLACE INTO base.owntracks_location (
                event_key,device_key,event_at,latitude,longitude,accuracy_m,
                velocity_kmh,battery_percent,
                object_key,storage_generation,received_at,loaded_at,parser_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                raw.logical_key,
                raw.subject_key,
                raw.observed_at,
                self.message["lat"],
                self.message["lon"],
                self.message.get("acc"),
                self.message.get("vel"),
                self.message.get("batt"),
                raw.key,
                raw.storage_generation,
                raw.storage_created_at,
                loaded_at,
                self.parser_version,
            ],
        )
