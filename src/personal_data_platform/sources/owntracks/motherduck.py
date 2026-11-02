"""MotherDuck persistence for source-reported OwnTracks locations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from personal_data_platform.raw.models import RawObject

from .batch import OwnTracksBatch

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def write_batch(connection: DuckDBPyConnection, raw: RawObject, batch: OwnTracksBatch) -> int:
    connection.execute(
        """INSERT INTO base.location_point (
            location_point_id, device_key, observed_at, latitude, longitude,
            accuracy_meters, speed_kmh, motion_activities
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT (location_point_id) DO UPDATE SET
            device_key=excluded.device_key, observed_at=excluded.observed_at,
            latitude=excluded.latitude, longitude=excluded.longitude,
            accuracy_meters=excluded.accuracy_meters, speed_kmh=excluded.speed_kmh,
            motion_activities=excluded.motion_activities""",
        [
            raw.logical_key,
            raw.subject_key,
            raw.observed_at,
            batch.message["lat"],
            batch.message["lon"],
            batch.message.get("acc"),
            batch.message.get("vel"),
            batch.message.get("motionactivities"),
        ],
    )
    return 1
