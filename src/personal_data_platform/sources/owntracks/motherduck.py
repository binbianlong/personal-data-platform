"""MotherDuck persistence for OwnTracks locations."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from personal_data_platform.raw.models import RawObject

from .batch import OwnTracksBatch

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def write_batch(
    connection: DuckDBPyConnection, raw: RawObject, batch: OwnTracksBatch, *, loaded_at: datetime
) -> None:
    connection.execute(
        """INSERT OR REPLACE INTO base.owntracks_location (
            event_key,device_key,event_at,latitude,longitude,accuracy_m,
            velocity_kmh,motionactivities,
            object_key,storage_generation,received_at,loaded_at,parser_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            raw.logical_key,
            raw.subject_key,
            raw.observed_at,
            batch.message["lat"],
            batch.message["lon"],
            batch.message.get("acc"),
            batch.message.get("vel"),
            batch.message.get("motionactivities"),
            raw.key,
            raw.storage_generation,
            raw.storage_created_at,
            loaded_at,
            batch.parser_version,
        ],
    )
