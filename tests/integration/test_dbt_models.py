from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from personal_data_platform.dbt_runner import run_dbt
from personal_data_platform.loader.models import ParsedScreenTimeRecord, RawObject
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


def _record(
    raw: RawObject,
    *,
    event_key: str,
    offset: int,
    bundle_id: str,
    event_at: datetime,
    foreground: bool,
) -> ParsedScreenTimeRecord:
    return ParsedScreenTimeRecord(
        event_key=event_key,
        object_key=raw.key,
        device_key=raw.device_key,
        source_stream=raw.stream,
        segment_key=raw.segment_key,
        segment_sha256=raw.sha256,
        observed_at=raw.observed_at,
        segment_filename="segment.segb",
        record_offset=offset,
        record_metadata_offset=offset,
        record_state="WRITTEN",
        segment_record_timestamp=raw.observed_at,
        crc_passed=True,
        transition_reason=None,
        kind=1,
        in_foreground=foreground,
        cf_absolute_time=1.0,
        event_at=event_at,
        bundle_id=bundle_id,
        app_version=None,
        app_build=None,
        platform_flag=2,
        unknown_field_count=0,
        original_payload=b"payload",
        parser_version="app-in-focus-v1",
    )


def test_dbt_pairs_events_and_splits_tokyo_midnight(tmp_path, monkeypatch) -> None:
    database = tmp_path / "dbt-test.duckdb"
    raw = RawObject(
        key="raw/screen_time/v1/device/App.InFocus/segment/2026-08-27T00:00:00Z/hash.segb.gz",
        device_key="device",
        stream="App.InFocus",
        segment_key="segment",
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        sha256="a" * 64,
    )
    records = [
        _record(
            raw,
            event_key="a-start",
            offset=1,
            bundle_id="app.a",
            event_at=datetime(2026, 8, 26, 14, 59, tzinfo=UTC),
            foreground=True,
        ),
        _record(
            raw,
            event_key="a-end",
            offset=2,
            bundle_id="app.a",
            event_at=datetime(2026, 8, 26, 15, 1, tzinfo=UTC),
            foreground=False,
        ),
        _record(
            raw,
            event_key="b-start",
            offset=3,
            bundle_id="app.b",
            event_at=datetime(2026, 8, 26, 16, 0, tzinfo=UTC),
            foreground=True,
        ),
        _record(
            raw,
            event_key="c-start",
            offset=4,
            bundle_id="app.c",
            event_at=datetime(2026, 8, 26, 16, 2, tzinfo=UTC),
            foreground=True,
        ),
        _record(
            raw,
            event_key="d-end",
            offset=5,
            bundle_id="app.d",
            event_at=datetime(2026, 8, 26, 16, 3, tzinfo=UTC),
            foreground=False,
        ),
    ]
    records.append(replace(records[0], record_offset=6, record_metadata_offset=6))
    deleted_start = replace(
        records[0],
        event_key="deleted-start",
        record_offset=7,
        record_metadata_offset=7,
        bundle_id="app.deleted",
    )
    records.extend(
        [
            deleted_start,
            replace(deleted_start, record_metadata_offset=8, record_state="DELETED"),
        ]
    )

    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    warehouse.migrate()
    warehouse.load_object(raw, byte_size=100, records=records)
    warehouse.close()

    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local")

    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_transition") == 5
        assert (
            warehouse.query_value(
                "SELECT count(*) FROM base.screen_time_transition WHERE event_key = 'deleted-start'"
            )
            == 0
        )
        assert (
            warehouse.query_value(
                "SELECT duplicate_occurrence_count FROM base.screen_time_transition "
                "WHERE event_key = 'a-start'"
            )
            == 1
        )
        assert dict(
            warehouse.query_rows(
                "SELECT quality, count(*) FROM base.screen_time_interval GROUP BY quality"
            )
        ) == {
            "complete": 1,
            "inferred_end_from_next_start": 1,
            "missing_end": 1,
            "missing_start": 1,
        }
        assert warehouse.query_rows(
            "SELECT activity_date, total_seconds FROM marts.daily_screen_time "
            "WHERE bundle_id = 'app.a' ORDER BY activity_date"
        ) == [(datetime(2026, 8, 26).date(), 60.0), (datetime(2026, 8, 27).date(), 60.0)]
        assert (
            warehouse.query_value(
                "SELECT total_seconds FROM marts.daily_screen_time WHERE bundle_id = 'app.b'"
            )
            == 120.0
        )
    finally:
        warehouse.close()
