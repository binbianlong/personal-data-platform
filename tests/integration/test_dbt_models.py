from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from personal_data_platform.dbt_runner import DBT_PROJECT_DIR, run_dbt
from personal_data_platform.loader.models import ParsedScreenTimeRecord, RawObject
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


@pytest.fixture
def dbt_project(tmp_path: Path) -> Path:
    project = tmp_path / "dbt"
    project.mkdir()
    for filename in ("dbt_project.yml", "profiles.yml"):
        shutil.copyfile(DBT_PROJECT_DIR / filename, project / filename)
    for directory in ("models", "macros", "tests"):
        shutil.copytree(DBT_PROJECT_DIR / directory, project / directory)
    return project


@pytest.fixture
def raw() -> RawObject:
    return RawObject(
        key="raw/screen_time/v1/device/App.InFocus/segment/2026-08-27T00:00:00Z/hash.segb.gz",
        device_key="device",
        stream="App.InFocus",
        segment_key="segment",
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        sha256="a" * 64,
    )


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


def test_dbt_pairs_events_and_splits_tokyo_midnight(
    tmp_path, monkeypatch, dbt_project, raw
) -> None:
    database = tmp_path / "dbt-test.duckdb"
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
    run_dbt(target="local", project_dir=dbt_project)

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
            "SELECT start_event_key, end_event_key, has_duplicate_source "
            "FROM base.screen_time_interval WHERE quality = 'complete'"
        ) == [("a-start", "a-end", True)]
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
        warehouse.connection.execute("SET TimeZone = 'UTC'")
        utc_keys = warehouse.query_rows(
            "SELECT interval_key FROM base.screen_time_interval ORDER BY interval_key"
        )
        warehouse.connection.execute("SET TimeZone = 'Asia/Tokyo'")
        assert (
            warehouse.query_rows(
                "SELECT interval_key FROM base.screen_time_interval ORDER BY interval_key"
            )
            == utc_keys
        )
    finally:
        warehouse.close()


@pytest.mark.parametrize(
    ("events", "expected_intervals", "expected_daily_seconds"),
    [
        pytest.param(
            [("start", 0, True, False), ("end", 60, False, True)],
            [("start", "end", "complete", 60.0, True)],
            (60.0, 0.0),
            id="duplicate-observed-end",
        ),
        pytest.param(
            [("first", 0, True, False), ("next", 60, True, True)],
            [
                ("first", None, "inferred_end_from_next_start", 60.0, True),
                ("next", None, "missing_end", None, True),
            ],
            (0.0, 60.0),
            id="duplicate-inferred-boundary",
        ),
        pytest.param(
            [
                ("first", 0, True, False),
                ("a-next", 60, True, False),
                ("b-end", 60, False, False),
            ],
            [
                ("first", None, "inferred_end_from_next_start", 60.0, False),
                ("a-next", "b-end", "complete", 0.0, False),
            ],
            (0.0, 60.0),
            id="tied-start-before-end",
        ),
        pytest.param(
            [
                ("first", 0, True, False),
                ("a-end", 60, False, False),
                ("b-next", 60, True, False),
            ],
            [
                ("first", "a-end", "complete", 60.0, False),
                ("b-next", None, "missing_end", None, False),
            ],
            (60.0, 0.0),
            id="tied-end-before-start",
        ),
        pytest.param(
            [
                ("a-end", 0, False, True),
                ("b-start", 0, True, False),
                ("c-end", 0, False, False),
            ],
            [
                ("b-start", "c-end", "complete", 0.0, False),
                (None, "a-end", "missing_start", None, True),
            ],
            (0.0, 0.0),
            id="tied-unmatched-end-keeps-identity",
        ),
    ],
)
def test_dbt_preserves_boundary_order_and_evidence(
    tmp_path,
    monkeypatch,
    dbt_project,
    raw,
    events,
    expected_intervals,
    expected_daily_seconds,
) -> None:
    database = tmp_path / "dbt-test.duckdb"
    records = []
    for event_key, seconds, foreground, duplicate in events:
        offset = len(records) + 1
        record = _record(
            raw,
            event_key=event_key,
            offset=offset,
            bundle_id="app.a",
            event_at=raw.observed_at + timedelta(seconds=seconds),
            foreground=foreground,
        )
        records.append(record)
        if duplicate:
            records.append(
                replace(record, record_offset=offset + 1, record_metadata_offset=offset + 1)
            )

    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        warehouse.migrate()
        warehouse.load_object(raw, byte_size=100, records=records)
    finally:
        warehouse.close()

    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project)

    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert (
            warehouse.query_rows(
                "SELECT start_event_key, end_event_key, quality, duration_seconds, "
                "has_duplicate_source FROM base.screen_time_interval "
                "ORDER BY started_at NULLS LAST, start_event_key"
            )
            == expected_intervals
        )
        assert warehouse.query_rows(
            "SELECT coalesce(sum(complete_seconds), 0), coalesce(sum(inferred_seconds), 0) "
            "FROM marts.daily_screen_time"
        ) == [expected_daily_seconds]
    finally:
        warehouse.close()


def test_dbt_views_follow_late_segment_corrections(tmp_path, monkeypatch, dbt_project, raw) -> None:
    database = tmp_path / "dbt-test.duckdb"
    records = [
        _record(
            raw,
            event_key="start",
            offset=1,
            bundle_id="app.a",
            event_at=raw.observed_at,
            foreground=True,
        ),
        _record(
            raw,
            event_key="old-end",
            offset=2,
            bundle_id="app.a",
            event_at=raw.observed_at + timedelta(seconds=120),
            foreground=False,
        ),
    ]
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        warehouse.migrate()
        warehouse.load_object(raw, byte_size=100, records=records)
    finally:
        warehouse.close()

    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project)

    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_value("SELECT total_seconds FROM marts.daily_screen_time") == 120
        correction = replace(
            raw,
            key="corrected-segment",
            observed_at=raw.observed_at + timedelta(days=1),
            sha256="b" * 64,
        )
        corrected_records = [
            replace(
                record,
                object_key=correction.key,
                observed_at=correction.observed_at,
                segment_sha256=correction.sha256,
            )
            for record in records
        ]
        corrected_records[1] = replace(
            corrected_records[1],
            event_key="corrected-end",
            event_at=raw.observed_at + timedelta(seconds=60),
        )
        warehouse.load_object(correction, byte_size=100, records=corrected_records)
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_transition") == 2
        assert warehouse.query_rows(
            "SELECT end_event_key, duration_seconds FROM base.screen_time_interval"
        ) == [("corrected-end", 60.0)]
        assert warehouse.query_value("SELECT total_seconds FROM marts.daily_screen_time") == 60
    finally:
        warehouse.close()
