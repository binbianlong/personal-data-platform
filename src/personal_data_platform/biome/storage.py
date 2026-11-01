"""Read only durable, unacknowledged Screen Time payloads."""

from __future__ import annotations

from collections.abc import Iterable

from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.contracts import RawCodec

from .collector_state import CollectorState


class BiomeLocalRepository:
    def __init__(self, *, state: CollectorState, source: RawCodec) -> None:
        if state.source_id != source.source_id:
            raise ValueError("collector state belongs to a different source")
        self.state = state
        self._source = source

    def list_raw(self, prefix: str) -> Iterable[RawObject]:
        for pending in self.state.pending(stream=self._source.stream):
            identity = pending.identity
            if identity.object_key.startswith(prefix):
                yield self._source.parse_raw_key(
                    identity.object_key,
                    storage_created_at=identity.observed_at,
                    storage_generation=1,
                )

    def get_raw(self, key: str, *, generation: int) -> bytes:
        if generation != 1:
            raise ValueError(f"Raw generation mismatch: {key}")
        for pending in self.state.pending(stream=self._source.stream):
            if pending.identity.object_key == key:
                return pending.compressed_payload
        raise ValueError(f"Raw observation is unavailable: {key}")
