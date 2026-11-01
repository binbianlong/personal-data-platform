from __future__ import annotations

import duckdb
import pytest

from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

# Public column names and DuckDB types are the writer and analytics boundary.
BASE_COLUMNS = {
    "sleep_session": (
        "sleep_session_id:VARCHAR start_at:TIMESTAMP_WITH_TIME_ZONE "
        "end_at:TIMESTAMP_WITH_TIME_ZONE source_date:DATE sleep_minutes:DOUBLE "
        "sleep_type:VARCHAR is_main_sleep:BOOLEAN awake_minutes:DOUBLE "
        "sleep_period_minutes:DOUBLE sleep_latency_minutes:DOUBLE "
        "after_wake_minutes:DOUBLE offset_seconds:INTEGER end_offset_seconds:INTEGER"
    ),
    "sleep_detail": (
        "sleep_detail_id:VARCHAR sleep_session_id:VARCHAR kind:VARCHAR "
        "start_at:TIMESTAMP_WITH_TIME_ZONE end_at:TIMESTAMP_WITH_TIME_ZONE "
        "category:VARCHAR offset_seconds:INTEGER end_offset_seconds:INTEGER"
    ),
    "heart_rate_minute": (
        "heart_rate_minute_id:VARCHAR start_at:TIMESTAMP_WITH_TIME_ZONE mean_bpm:DOUBLE "
        "min_bpm:DOUBLE max_bpm:DOUBLE aggregation_version:VARCHAR"
    ),
    "steps_interval": (
        "steps_interval_id:VARCHAR start_at:TIMESTAMP_WITH_TIME_ZONE "
        "end_at:TIMESTAMP_WITH_TIME_ZONE source_date:DATE steps:DOUBLE "
        "offset_seconds:INTEGER end_offset_seconds:INTEGER"
    ),
    "active_zone_interval": (
        "active_zone_interval_id:VARCHAR start_at:TIMESTAMP_WITH_TIME_ZONE "
        "end_at:TIMESTAMP_WITH_TIME_ZONE source_date:DATE active_zone_minutes:DOUBLE "
        "category:VARCHAR offset_seconds:INTEGER end_offset_seconds:INTEGER"
    ),
    "exercise_session": (
        "exercise_session_id:VARCHAR start_at:TIMESTAMP_WITH_TIME_ZONE "
        "end_at:TIMESTAMP_WITH_TIME_ZONE source_date:DATE exercise_type:VARCHAR "
        "exercise_name:VARCHAR active_seconds:DOUBLE offset_seconds:INTEGER "
        "end_offset_seconds:INTEGER"
    ),
    "sedentary_interval": (
        "sedentary_interval_id:VARCHAR start_at:TIMESTAMP_WITH_TIME_ZONE "
        "end_at:TIMESTAMP_WITH_TIME_ZONE source_date:DATE offset_seconds:INTEGER "
        "end_offset_seconds:INTEGER"
    ),
    "distance_interval": (
        "distance_interval_id:VARCHAR start_at:TIMESTAMP_WITH_TIME_ZONE "
        "end_at:TIMESTAMP_WITH_TIME_ZONE source_date:DATE distance_meters:DOUBLE "
        "offset_seconds:INTEGER end_offset_seconds:INTEGER"
    ),
    "heart_rate_zone_interval": (
        "heart_rate_zone_interval_id:VARCHAR start_at:TIMESTAMP_WITH_TIME_ZONE "
        "end_at:TIMESTAMP_WITH_TIME_ZONE source_date:DATE zone:VARCHAR "
        "offset_seconds:INTEGER end_offset_seconds:INTEGER"
    ),
    "health_daily": (
        "source_date:DATE resting_heart_rate_bpm:DOUBLE hrv_rmssd_ms:DOUBLE "
        "deep_sleep_hrv_rmssd_ms:DOUBLE non_rem_heart_rate_bpm:BIGINT hrv_entropy:DOUBLE "
        "respiratory_rate_breaths_per_minute:DOUBLE spo2_average_percent:DOUBLE "
        "spo2_lower_bound_percent:DOUBLE spo2_upper_bound_percent:DOUBLE "
        "spo2_standard_deviation_percent:DOUBLE nightly_skin_temperature_celsius:DOUBLE "
        "baseline_skin_temperature_celsius:DOUBLE "
        "relative_nightly_stddev_30d_celsius:DOUBLE vo2_max_ml_kg_min:DOUBLE "
        "vo2_max_estimated:BOOLEAN cardio_fitness_level:VARCHAR vo2_max_covariance:DOUBLE"
    ),
    "hrv_sample": (
        "hrv_sample_id:VARCHAR sample_at:TIMESTAMP_WITH_TIME_ZONE source_date:DATE "
        "rmssd_ms:DOUBLE sdnn_ms:DOUBLE offset_seconds:INTEGER"
    ),
    "sleep_respiratory_summary": (
        "respiratory_summary_id:VARCHAR sample_at:TIMESTAMP_WITH_TIME_ZONE "
        "source_date:DATE full_breaths_per_minute:DOUBLE full_standard_deviation:DOUBLE "
        "full_signal_to_noise:DOUBLE deep_breaths_per_minute:DOUBLE "
        "deep_standard_deviation:DOUBLE deep_signal_to_noise:DOUBLE "
        "light_breaths_per_minute:DOUBLE light_standard_deviation:DOUBLE "
        "light_signal_to_noise:DOUBLE rem_breaths_per_minute:DOUBLE "
        "rem_standard_deviation:DOUBLE rem_signal_to_noise:DOUBLE offset_seconds:INTEGER"
    ),
    "location_point": (
        "location_point_id:VARCHAR device_key:VARCHAR "
        "observed_at:TIMESTAMP_WITH_TIME_ZONE latitude:DOUBLE longitude:DOUBLE "
        "accuracy_meters:DOUBLE speed_kmh:DOUBLE motion_activities:VARCHAR[]"
    ),
    "iphone_screen_time_event": (
        "event_key:VARCHAR device_key:VARCHAR bundle_id:VARCHAR "
        "event_at:TIMESTAMP_WITH_TIME_ZONE state:VARCHAR is_valid:BOOLEAN"
    ),
    "mac_screen_time_event": (
        "event_key:VARCHAR device_key:VARCHAR bundle_id:VARCHAR "
        "event_at:TIMESTAMP_WITH_TIME_ZONE state:VARCHAR is_valid:BOOLEAN"
    ),
    "app": ("platform:VARCHAR bundle_id:VARCHAR app_name:VARCHAR"),
    "profile": (
        "profile_id:VARCHAR sex:VARCHAR height_cm:DOUBLE birth_date:DATE "
        "home_place_id:VARCHAR university_place_id:VARCHAR"
    ),
    "place": (
        "place_id:VARCHAR place_name:VARCHAR latitude:DOUBLE longitude:DOUBLE radius_meters:DOUBLE"
    ),
}


@pytest.fixture
def warehouse():
    value = Warehouse(connect(WarehouseConfig(":memory:")))
    value.migrate()
    try:
        yield value
    finally:
        value.close()


def test_catalog_exposes_only_canonical_base_and_ops_tables(warehouse):
    tables = warehouse.query_rows(
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name=current_database() AND schema_name IN ('base','ops','marts')"
    )
    assert set(tables) == {("base", name) for name in BASE_COLUMNS} | {
        ("ops", "job_lock"),
        ("ops", "schema_migration"),
    }
    assert (
        warehouse.query_value(
            "SELECT count(*) FROM duckdb_views() WHERE database_name=current_database() "
            "AND schema_name IN ('base','ops','marts')"
        )
        == 0
    )


@pytest.mark.parametrize("table,contract", BASE_COLUMNS.items())
def test_base_columns_preserve_exact_provider_grain_and_types(warehouse, table, contract):
    columns = warehouse.query_rows(
        "SELECT column_name, data_type FROM duckdb_columns() "
        "WHERE database_name=current_database() AND schema_name='base' "
        "AND table_name=? ORDER BY column_index",
        [table],
    )
    assert columns == [
        tuple(item.replace("_WITH_TIME_ZONE", " WITH TIME ZONE").split(":"))
        for item in contract.split()
    ]


def test_every_table_and_column_has_a_catalog_comment(warehouse):
    for catalog in ["duckdb_tables()", "duckdb_columns()"]:
        assert (
            warehouse.query_rows(
                f"SELECT table_name FROM {catalog} WHERE database_name=current_database() "
                "AND schema_name IN ('base','ops') AND (comment IS NULL OR length(trim(comment))=0)"
            )
            == []
        )


@pytest.mark.parametrize("source", ["iphone", "mac"])
def test_screen_time_accepts_unregistered_apps_and_preserves_fractional_event_time(
    warehouse, source
):
    table = f"base.{source}_screen_time_event"
    warehouse.connection.execute(
        f"INSERT INTO {table} VALUES ('event','device','unknown.app',"
        "TIMESTAMPTZ '2026-10-31T23:59:59.125Z','start',TRUE)"
    )
    assert warehouse.query_value(f"SELECT microsecond(event_at) FROM {table}") == 59125000
    with pytest.raises(duckdb.ConstraintException):
        warehouse.connection.execute(f"INSERT INTO {table} SELECT * FROM {table}")
    with pytest.raises(duckdb.ConstraintException):
        warehouse.connection.execute(f"UPDATE {table} SET state='unknown'")


def test_provider_unknowns_and_missing_daily_metrics_are_not_filled(warehouse):
    warehouse.connection.execute(
        "INSERT INTO base.health_daily (source_date) VALUES ('2026-10-31')"
    )
    assert warehouse.query_rows(
        "SELECT resting_heart_rate_bpm,vo2_max_estimated FROM base.health_daily"
    ) == [(None, None)]
    for identifier, activities in [("missing", None), ("empty", []), ("unknown", ["new-label"])]:
        warehouse.connection.execute(
            "INSERT INTO base.location_point (location_point_id,motion_activities) VALUES (?,?)",
            [identifier, activities],
        )
    assert warehouse.query_rows(
        "SELECT motion_activities FROM base.location_point ORDER BY location_point_id"
    ) == [([],), (None,), (["new-label"],)]


def test_sleep_parent_and_detail_can_be_replaced_in_one_transaction(warehouse):
    warehouse.connection.execute("INSERT INTO base.sleep_session (sleep_session_id) VALUES ('s')")
    warehouse.connection.execute(
        "INSERT INTO base.sleep_detail (sleep_detail_id,sleep_session_id,category) "
        "VALUES ('d','s','unknown-stage')"
    )
    warehouse.connection.execute("BEGIN")
    warehouse.connection.execute("DELETE FROM base.sleep_detail")
    warehouse.connection.execute("DELETE FROM base.sleep_session")
    warehouse.connection.execute("INSERT INTO base.sleep_session (sleep_session_id) VALUES ('s')")
    warehouse.connection.execute(
        "INSERT INTO base.sleep_detail (sleep_detail_id,sleep_session_id,category) "
        "VALUES ('d','s','new-stage')"
    )
    warehouse.connection.execute("COMMIT")
    assert warehouse.query_rows(
        "SELECT category FROM base.sleep_detail JOIN base.sleep_session USING (sleep_session_id)"
    ) == [("new-stage",)]


def test_ops_columns_only_store_lease_and_migration_state(warehouse):
    assert warehouse.query_rows(
        "SELECT table_name,column_name,data_type FROM duckdb_columns() "
        "WHERE database_name=current_database() AND schema_name='ops' "
        "ORDER BY table_name,column_index"
    ) == [
        ("job_lock", "job_name", "VARCHAR"),
        ("job_lock", "owner_id", "VARCHAR"),
        ("job_lock", "expires_at", "TIMESTAMP WITH TIME ZONE"),
        ("schema_migration", "migration_id", "VARCHAR"),
        ("schema_migration", "checksum", "VARCHAR"),
        ("schema_migration", "applied_at", "TIMESTAMP WITH TIME ZONE"),
    ]


def test_app_identity_is_platform_and_bundle_id_without_seeded_values(warehouse):
    assert warehouse.query_value("SELECT count(*) FROM base.app") == 0
    assert warehouse.query_value("SELECT count(*) FROM base.profile") == 0
    assert warehouse.query_value("SELECT count(*) FROM base.place") == 0
    warehouse.connection.execute(
        "INSERT INTO base.app VALUES ('ios','same.bundle','Phone app'),"
        "('macos','same.bundle','Mac app')"
    )
    assert warehouse.query_value("SELECT count(*) FROM base.app") == 2
    with pytest.raises(duckdb.ConstraintException):
        warehouse.connection.execute(
            "INSERT INTO base.app VALUES ('ios','same.bundle','Duplicate')"
        )
