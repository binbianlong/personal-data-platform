"""Normalized screen_time data passed to warehouse implementations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import ParsedScreenTimeRecord
from .parser import PARSER_VERSION


@dataclass(frozen=True, slots=True)
class ScreenTimeBatch:
    records: Sequence[ParsedScreenTimeRecord]
    segment_kind: str | None = None
    parser_version_override: str | None = None

    @property
    def parser_version(self) -> str:
        return (
            self.records[0].parser_version
            if self.records
            else (self.parser_version_override or PARSER_VERSION)
        )

    @property
    def record_count(self) -> int:
        return len(self.records)
