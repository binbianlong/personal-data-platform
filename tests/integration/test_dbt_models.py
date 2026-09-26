from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from personal_data_platform.dbt_runner import DBT_PROJECT_DIR, run_dbt
from personal_data_platform.raw.models import RawObject
from personal_data_platform.sources.screen_time.models import ParsedScreenTimeRecord
from personal_data_platform.sources.screen_time.writer import ScreenTimeBatch
from personal_data_platform.storage.motherduck import (
    Warehouse,
    WarehouseConfig,
    connect,
)


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
        source_id="screen_time",
        schema_version=1,
        subject_key="device",
        stream="App.InFocus",
        logical_key="segment",
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        sha256="a" * 64,
        storage_created_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        storage_generation=1,
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
        device_key=raw.subject_key,
        source_stream=raw.stream,
        segment_key=raw.logical_key,
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


def test_mac_daily_totals_preserve_observed_end_at_next_start(
    tmp_path, monkeypatch, dbt_project, raw
) -> None:
    database = tmp_path / "mac-daily.duckdb"
    mac_raw = replace(
        raw, key="raw/mac", subject_key="mac-device", stream="app-usage", logical_key="mac-segment"
    )
    before_midnight = datetime(2026, 8, 26, 14, 59, tzinfo=UTC)
    midnight_plus_one = datetime(2026, 8, 26, 15, 1, tzinfo=UTC)
    mac_records = [
        _record(
            mac_raw,
            event_key="first-start",
            offset=1,
            bundle_id="mac.app",
            event_at=before_midnight,
            foreground=True,
        ),
        _record(
            mac_raw,
            event_key="z-end",
            offset=2,
            bundle_id="mac.app",
            event_at=midnight_plus_one,
            foreground=False,
        ),
        _record(
            mac_raw,
            event_key="a-next-start",
            offset=3,
            bundle_id="mac.app",
            event_at=midnight_plus_one,
            foreground=True,
        ),
        _record(
            mac_raw,
            event_key="last-end",
            offset=4,
            bundle_id="mac.app",
            event_at=midnight_plus_one + timedelta(minutes=2),
            foreground=False,
        ),
    ]
    phone_records = [
        _record(
            raw,
            event_key="phone-start",
            offset=1,
            bundle_id="phone.app",
            event_at=before_midnight,
            foreground=True,
        ),
        _record(
            raw,
            event_key="phone-end",
            offset=2,
            bundle_id="phone.app",
            event_at=before_midnight + timedelta(minutes=1),
            foreground=False,
        ),
    ]
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    warehouse.migrate()
    warehouse.load_object(raw, byte_size=100, batch=ScreenTimeBatch(phone_records))
    warehouse.load_object(
        mac_raw,
        byte_size=100,
        batch=ScreenTimeBatch(
            [replace(record, parser_version="app-usage-v1") for record in mac_records]
        ),
    )
    warehouse.close()

    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project, selector="tag:screen_time")

    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_rows(
            "SELECT start_event_key, end_event_key, quality FROM base.screen_time_interval "
            "WHERE platform = 'macos' ORDER BY started_at"
        ) == [
            ("first-start", "z-end", "complete"),
            ("a-next-start", "last-end", "complete"),
        ]
        assert warehouse.query_rows(
            "SELECT activity_date, platform, total_seconds FROM marts.daily_screen_time_total "
            "ORDER BY platform, activity_date"
        ) == [
            (datetime(2026, 8, 26).date(), "ios", 60.0),
            (datetime(2026, 8, 26).date(), "macos", 60.0),
            (datetime(2026, 8, 27).date(), "macos", 180.0),
        ]
    finally:
        warehouse.close()


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
    warehouse.load_object(raw, byte_size=100, batch=ScreenTimeBatch(records))
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
        daily_totals = warehouse.query_rows(
            "SELECT activity_date, complete_seconds, inferred_seconds, total_seconds, "
            "complete_interval_parts, inferred_interval_parts "
            "FROM marts.daily_screen_time_total ORDER BY activity_date"
        )
        assert daily_totals == [
            (datetime(2026, 8, 26).date(), 60.0, 0.0, 60.0, 1, 0),
            (datetime(2026, 8, 27).date(), 60.0, 120.0, 180.0, 1, 1),
        ]
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
        assert (
            warehouse.query_rows(
                "SELECT activity_date, complete_seconds, inferred_seconds, total_seconds, "
                "complete_interval_parts, inferred_interval_parts "
                "FROM marts.daily_screen_time_total ORDER BY activity_date"
            )
            == daily_totals
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
                ("first", "b-end", "complete", 60.0, False),
                ("a-next", None, "missing_end", None, False),
            ],
            (60.0, 0.0),
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
                ("b-start", None, "missing_end", None, False),
                (None, "a-end", "missing_start", None, True),
                (None, "c-end", "missing_start", None, False),
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
        warehouse.load_object(raw, byte_size=100, batch=ScreenTimeBatch(records))
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
                "ORDER BY started_at NULLS LAST, start_event_key, end_event_key"
            )
            == expected_intervals
        )
        assert warehouse.query_rows(
            "SELECT coalesce(sum(complete_seconds), 0), coalesce(sum(inferred_seconds), 0) "
            "FROM marts.daily_screen_time"
        ) == [expected_daily_seconds]
        assert warehouse.query_rows(
            "SELECT coalesce(sum(complete_seconds), 0), coalesce(sum(inferred_seconds), 0) "
            "FROM marts.daily_screen_time_total"
        ) == [expected_daily_seconds]
        if expected_daily_seconds == (0.0, 0.0):
            assert warehouse.query_value("SELECT count(*) FROM marts.daily_screen_time_total") == 0
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
        warehouse.load_object(raw, byte_size=100, batch=ScreenTimeBatch(records))
    finally:
        warehouse.close()

    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project)

    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_value("SELECT total_seconds FROM marts.daily_screen_time") == 120
        assert (
            warehouse.query_value("SELECT total_seconds FROM marts.daily_screen_time_total") == 120
        )
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
        warehouse.load_object(correction, byte_size=100, batch=ScreenTimeBatch(corrected_records))
        assert warehouse.query_value("SELECT count(*) FROM base.screen_time_transition") == 2
        assert warehouse.query_rows(
            "SELECT end_event_key, duration_seconds FROM base.screen_time_interval"
        ) == [("corrected-end", 60.0)]
        assert warehouse.query_value("SELECT total_seconds FROM marts.daily_screen_time") == 60
        assert (
            warehouse.query_value("SELECT total_seconds FROM marts.daily_screen_time_total") == 60
        )

        deletion = replace(
            correction,
            key="deleted-segment",
            observed_at=correction.observed_at + timedelta(days=1),
            sha256="c" * 64,
        )
        warehouse.load_object(deletion, byte_size=0, batch=ScreenTimeBatch([]))
        assert warehouse.query_value("SELECT count(*) FROM marts.daily_screen_time") == 0
        assert warehouse.query_value("SELECT count(*) FROM marts.daily_screen_time_total") == 0
    finally:
        warehouse.close()


def test_daily_totals_sum_apps_without_combining_devices(tmp_path, monkeypatch, dbt_project, raw):
    database = tmp_path / "daily-totals.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        warehouse.migrate()
        for device, durations in [("first", [60, 90]), ("second", [45])]:
            device_raw = replace(raw, key=f"raw-{device}", subject_key=device)
            records = []
            for index, duration in enumerate(durations):
                start_at = raw.observed_at + timedelta(minutes=index * 10)
                for foreground, event_at in [
                    (True, start_at),
                    (False, start_at + timedelta(seconds=duration)),
                ]:
                    records.append(
                        _record(
                            device_raw,
                            event_key=f"{device}-{index}-{foreground}",
                            offset=len(records) + 1,
                            bundle_id=f"app.{index}",
                            event_at=event_at,
                            foreground=foreground,
                        )
                    )
            warehouse.load_object(device_raw, byte_size=100, batch=ScreenTimeBatch(records))
    finally:
        warehouse.close()

    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project, selector="tag:screen_time")

    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_rows(
            "SELECT activity_date, device_key, platform, complete_seconds, inferred_seconds, "
            "total_seconds, complete_interval_parts, inferred_interval_parts "
            "FROM marts.daily_screen_time_total ORDER BY device_key"
        ) == [
            (raw.observed_at.date(), "first", "ios", 150.0, 0.0, 150.0, 2, 0),
            (raw.observed_at.date(), "second", "ios", 45.0, 0.0, 45.0, 1, 0),
        ]
    finally:
        warehouse.close()


@pytest.mark.parametrize("with_missing_boundaries", [False, True])
def test_daily_totals_do_not_fill_missing_usage(
    tmp_path, monkeypatch, dbt_project, raw, with_missing_boundaries
):
    database = tmp_path / "empty-daily-totals.duckdb"
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        warehouse.migrate()
        if with_missing_boundaries:
            records = [
                _record(
                    raw,
                    event_key=f"event-{index}",
                    offset=index + 1,
                    bundle_id=f"app.{index}",
                    event_at=raw.observed_at + timedelta(minutes=index),
                    foreground=foreground,
                )
                for index, foreground in enumerate([True, False])
            ]
            warehouse.load_object(raw, byte_size=100, batch=ScreenTimeBatch(records))
    finally:
        warehouse.close()

    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    run_dbt(target="local", project_dir=dbt_project)

    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_value("SELECT count(*) FROM marts.daily_screen_time_total") == 0
    finally:
        warehouse.close()
