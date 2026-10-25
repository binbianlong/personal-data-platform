"""OwnTracks location source contract for loading, auditing and rebuilding."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import DecodedBatch, RawRepository, SourceHealth

from .raw import RAW_PREFIX, location_message, parse_raw_key


class OwnTracksSource:
    source_id = "owntracks"
    stream = "location"
    raw_storage: Literal["gcs", "sqlite"] = "gcs"
    schema_versions: tuple[int, ...] = (1,)
    parser_version = "owntracks-location-v2"
    raw_prefixes: tuple[str, ...] = (RAW_PREFIX,)
    raw_suffixes: tuple[str, ...] = (".json.gz",)
    required_relations: tuple[str, ...] = (
        "base.owntracks_location",
        "marts.owntracks_location_time_series",
    )
    dbt_selector = "tag:owntracks"
    monitor_name = "owntracks_reconciliation"

    def validate_raw_key(self, key: str) -> None:
        parse_raw_key(key, storage_created_at=datetime.now(UTC), storage_generation=1)

    def parse_raw_key(
        self, key: str, *, storage_created_at: datetime, storage_generation: int
    ) -> RawObject:
        return parse_raw_key(
            key, storage_created_at=storage_created_at, storage_generation=storage_generation
        )

    def decode(self, raw: RawObject, payload: bytes) -> DecodedBatch:
        from .writer import OwnTracksBatch

        if (raw.source_id, raw.stream, raw.schema_version) != (self.source_id, self.stream, 1):
            raise ValueError("Raw does not belong to OwnTracks location")
        self.validate_raw_key(raw.key)
        message = location_message(payload, from_raw=True)
        if message is None:
            raise ValueError("OwnTracks Raw must contain a location")
        timestamp = message["tst"]
        assert isinstance(timestamp, int)
        if datetime.fromtimestamp(timestamp, UTC) != raw.observed_at:
            raise ValueError("OwnTracks Raw timestamp does not match payload")
        return OwnTracksBatch(message, parser_version=self.parser_version)

    def repository_from_env(self) -> RawRepository:
        from personal_data_platform.storage.gcs import GCSRawRepository

        return GCSRawRepository.from_env(source=self)

    def audit(
        self, repository: RawRepository, observations: Sequence[RawObject], now: datetime
    ) -> SourceHealth:
        return SourceHealth(ok=True, details=self.inventory(observations))

    def inventory(self, observations: Sequence[RawObject]) -> dict[str, object]:
        return {
            "device_count": len({raw.subject_key for raw in observations}),
            "location_object_count": len(observations),
        }
