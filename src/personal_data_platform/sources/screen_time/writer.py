"""Write decoded Screen Time rows inside the shared ingestion transaction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from personal_data_platform.raw.models import RawObject

from .models import ParsedScreenTimeRecord
from .parser import PARSER_VERSION


@dataclass(frozen=True, slots=True)
class ScreenTimeBatch:
    records: Sequence[ParsedScreenTimeRecord]
    source_segment_name: str | None = None
    segment_kind: str | None = None

    @property
    def parser_version(self) -> str:
        return self.records[0].parser_version if self.records else PARSER_VERSION

    @property
    def record_count(self) -> int:
        return len(self.records)

    def write(
        self, connection: Any, raw: RawObject, *, byte_size: int, loaded_at: datetime
    ) -> None:
        raise RuntimeError("Screen Time batches require the ingestion checkpoint coordinator")
