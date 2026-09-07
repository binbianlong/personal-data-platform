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

    @property
    def parser_version(self) -> str:
        return self.records[0].parser_version if self.records else PARSER_VERSION

    @property
    def record_count(self) -> int:
        return len(self.records)

    def write(
        self, connection: Any, raw: RawObject, *, byte_size: int, loaded_at: datetime
    ) -> None:
        connection.execute(
            "DELETE FROM base.screen_time_record_occurrence WHERE object_key = ?", [raw.key]
        )
        if self.records:
            connection.executemany(
                """
                INSERT INTO base.screen_time_record_occurrence VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [_record_row(record, loaded_at) for record in self.records],
            )
        connection.execute(
            """
            INSERT INTO base.screen_time_segment_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (object_key) DO UPDATE SET
                record_count = excluded.record_count,
                parser_version = excluded.parser_version,
                loaded_at = excluded.loaded_at
            """,
            [
                raw.key,
                raw.subject_key,
                raw.stream,
                raw.logical_key,
                raw.observed_at,
                raw.sha256,
                byte_size,
                self.record_count,
                self.parser_version,
                loaded_at,
            ],
        )


def _record_row(record: ParsedScreenTimeRecord, loaded_at: datetime) -> list[Any]:
    return [
        record.object_key,
        record.record_offset,
        record.record_metadata_offset,
        record.event_key,
        record.device_key,
        record.source_stream,
        record.segment_key,
        record.segment_sha256,
        record.observed_at,
        record.segment_filename,
        record.record_state,
        record.segment_record_timestamp,
        record.crc_passed,
        record.transition_reason,
        record.kind,
        record.in_foreground,
        record.cf_absolute_time,
        record.event_at,
        record.bundle_id,
        record.app_version,
        record.app_build,
        record.platform_flag,
        record.unknown_field_count,
        record.original_payload,
        record.parser_version,
        loaded_at,
    ]
