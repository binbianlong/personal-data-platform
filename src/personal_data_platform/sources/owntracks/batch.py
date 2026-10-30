"""Normalized owntracks data passed to warehouse implementations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OwnTracksBatch:
    message: dict[str, object]
    parser_version: str

    @property
    def record_count(self) -> int:
        return 1
