import re
import sqlite3
from typing import Any

LEGACY_SQLITE_SCHEMA = "\nCREATE TABLE segment_observation (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    device_key TEXT NOT NULL,\n    stream TEXT NOT NULL,\n    segment_key TEXT NOT NULL,\n    observed_at TEXT NOT NULL,\n    sha256 TEXT NOT NULL,\n    object_key TEXT NOT NULL UNIQUE,\n    compressed_payload BLOB,\n    status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded')),\n    uploaded_at TEXT,\n    storage_created_at TEXT,\n    storage_generation INTEGER NOT NULL DEFAULT 1\n);\nCREATE INDEX segment_observation_latest\n    ON segment_observation (device_key, stream, segment_key, id DESC);\nCREATE INDEX segment_observation_pending ON segment_observation (status, id);\nCREATE TABLE collector_stream_scan (\n    stream TEXT PRIMARY KEY,\n    completed_at TEXT NOT NULL,\n    device_keys TEXT NOT NULL,\n    device_scanned_at TEXT NOT NULL\n);\n"
TABLES = ("segment_observation", "collector_stream_scan", "sqlite_sequence")

LEGACY_CHECKSUMS = {
    "001_initial.sql": "9f6f4ee0d01009caac246da5ad980762efb92b20c502bac94b92fa64d3ea200c",
    "002_owntracks.sql": "ece4bfe833f312942d8a205881c287987102cab0f51527475cea291a385d02db",
    "003_screen_time_current_state.sql": "174c6ca37ec45206736de52eceb6955ed9d893d86be67a8dc3d7dae77f9e6075",
    "004_screen_time_analysis_columns.sql": "75b72f43d3fa8e6f5a2502c9e4b68bcbb8c081edcba996a193676b0cb47812b8",
    "005_fitbit_minute_columns.sql": "8fbdb6061e9051f29d624ea351ce826dea5d38c3c11753c396df5bf23f54e392",
    "006_owntracks_without_battery.sql": "1b8edb1a727398a7e48e215bc396c02c3004bacd4508b779d2eb043968f73d12",
    "007_operational_state.sql": "e808bee4349e5aba7f4c8340fbe7e8858d85565a366f0d3c0e15becb8805611a",
    "008_terminal_ingestion_receipts.sql": "683f3cd120a4c111d238666272bb3fe59a0ad9c520d7d841b6f805d0bcb48bc9",
    "009_owntracks_motionactivities.sql": "75b94c91dff5e3ae44445d622f45c35c93f10dead1bcf0d1ca935cbef2d5c370",
    "010_screen_time_without_segment_filename.sql": "76cfce434b21238cde4a9bb3a55140e9ccb3b5b79757c7f28b588dc8b5fdcc43",
    "011_fitbit_health_metrics.sql": "8278987b415c9c4eda17251ed2d048d71c84d316f624935b1e681c341274ae56",
    "012_split_screen_time_sources.sql": "fae9722300a2d47885204abea07147fbd0da692c1716b1818dcdcb05aadcbc60",
}
LEGACY_COLUMNS = [
    ("base", "fitbit_activity_interval", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_activity_interval", "metric", "VARCHAR", "NO"),
    ("base", "fitbit_activity_interval", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_activity_interval", "start_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_activity_interval", "end_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_activity_interval", "value", "DOUBLE", "NO"),
    ("base", "fitbit_activity_interval", "offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_activity_interval", "end_offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_activity_interval", "source_date", "DATE", "YES"),
    ("base", "fitbit_activity_interval", "category", "VARCHAR", "YES"),
    ("base", "fitbit_activity_interval", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_activity_interval", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_activity_interval", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_distance_interval", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_distance_interval", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_distance_interval", "start_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_distance_interval", "end_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_distance_interval", "offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_distance_interval", "end_offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_distance_interval", "source_date", "DATE", "NO"),
    ("base", "fitbit_distance_interval", "distance_meters", "DOUBLE", "NO"),
    ("base", "fitbit_distance_interval", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_distance_interval", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_distance_interval", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_exercise", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_exercise", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_exercise", "start_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_exercise", "end_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_exercise", "offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_exercise", "end_offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_exercise", "source_date", "DATE", "NO"),
    ("base", "fitbit_exercise", "exercise_type", "VARCHAR", "NO"),
    ("base", "fitbit_exercise", "display_name", "VARCHAR", "NO"),
    ("base", "fitbit_exercise", "active_duration_seconds", "DOUBLE", "YES"),
    ("base", "fitbit_exercise", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_exercise", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_exercise", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_heart_rate_minute", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_heart_rate_minute", "data_source_family", "VARCHAR", "NO"),
    ("base", "fitbit_heart_rate_minute", "start_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_heart_rate_minute", "aggregation_version", "VARCHAR", "NO"),
    ("base", "fitbit_heart_rate_minute", "average", "DOUBLE", "NO"),
    ("base", "fitbit_heart_rate_minute", "minimum", "DOUBLE", "NO"),
    ("base", "fitbit_heart_rate_minute", "maximum", "DOUBLE", "NO"),
    ("base", "fitbit_heart_rate_minute", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_heart_rate_minute", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_heart_rate_minute", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_heart_rate_zone_interval", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_heart_rate_zone_interval", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_heart_rate_zone_interval", "start_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_heart_rate_zone_interval", "end_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_heart_rate_zone_interval", "offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_heart_rate_zone_interval", "end_offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_heart_rate_zone_interval", "source_date", "DATE", "NO"),
    ("base", "fitbit_heart_rate_zone_interval", "zone", "VARCHAR", "NO"),
    ("base", "fitbit_heart_rate_zone_interval", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_heart_rate_zone_interval", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_heart_rate_zone_interval", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_hrv_daily", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_hrv_daily", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_hrv_daily", "source_date", "DATE", "NO"),
    ("base", "fitbit_hrv_daily", "rmssd_ms", "DOUBLE", "YES"),
    ("base", "fitbit_hrv_daily", "deep_sleep_rmssd_ms", "DOUBLE", "YES"),
    ("base", "fitbit_hrv_daily", "non_rem_heart_rate_bpm", "BIGINT", "YES"),
    ("base", "fitbit_hrv_daily", "entropy", "DOUBLE", "YES"),
    ("base", "fitbit_hrv_daily", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_hrv_daily", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_hrv_daily", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_hrv_sample", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_hrv_sample", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_hrv_sample", "sample_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_hrv_sample", "offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_hrv_sample", "source_date", "DATE", "NO"),
    ("base", "fitbit_hrv_sample", "rmssd_ms", "DOUBLE", "YES"),
    ("base", "fitbit_hrv_sample", "sdnn_ms", "DOUBLE", "YES"),
    ("base", "fitbit_hrv_sample", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_hrv_sample", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_hrv_sample", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_oxygen_saturation_daily", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_oxygen_saturation_daily", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_oxygen_saturation_daily", "source_date", "DATE", "NO"),
    ("base", "fitbit_oxygen_saturation_daily", "average_percent", "DOUBLE", "NO"),
    ("base", "fitbit_oxygen_saturation_daily", "lower_bound_percent", "DOUBLE", "NO"),
    ("base", "fitbit_oxygen_saturation_daily", "upper_bound_percent", "DOUBLE", "NO"),
    ("base", "fitbit_oxygen_saturation_daily", "standard_deviation_percent", "DOUBLE", "YES"),
    ("base", "fitbit_oxygen_saturation_daily", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_oxygen_saturation_daily", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_oxygen_saturation_daily", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_respiratory_rate_daily", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_respiratory_rate_daily", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_respiratory_rate_daily", "source_date", "DATE", "NO"),
    ("base", "fitbit_respiratory_rate_daily", "breaths_per_minute", "DOUBLE", "NO"),
    ("base", "fitbit_respiratory_rate_daily", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_respiratory_rate_daily", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_respiratory_rate_daily", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "record_id", "VARCHAR", "NO"),
    (
        "base",
        "fitbit_respiratory_rate_sleep_summary",
        "sample_at",
        "TIMESTAMP WITH TIME ZONE",
        "NO",
    ),
    ("base", "fitbit_respiratory_rate_sleep_summary", "offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "source_date", "DATE", "NO"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "full_breaths_per_minute", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "full_standard_deviation", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "full_signal_to_noise", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "deep_breaths_per_minute", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "deep_standard_deviation", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "deep_signal_to_noise", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "light_breaths_per_minute", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "light_standard_deviation", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "light_signal_to_noise", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "rem_breaths_per_minute", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "rem_standard_deviation", "DOUBLE", "YES"),
    ("base", "fitbit_respiratory_rate_sleep_summary", "rem_signal_to_noise", "DOUBLE", "YES"),
    (
        "base",
        "fitbit_respiratory_rate_sleep_summary",
        "fetched_at",
        "TIMESTAMP WITH TIME ZONE",
        "NO",
    ),
    ("base", "fitbit_respiratory_rate_sleep_summary", "source_key", "VARCHAR", "NO"),
    (
        "base",
        "fitbit_respiratory_rate_sleep_summary",
        "loaded_at",
        "TIMESTAMP WITH TIME ZONE",
        "NO",
    ),
    ("base", "fitbit_resting_heart_rate_daily", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_resting_heart_rate_daily", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_resting_heart_rate_daily", "source_date", "DATE", "NO"),
    ("base", "fitbit_resting_heart_rate_daily", "beats_per_minute", "DOUBLE", "NO"),
    ("base", "fitbit_resting_heart_rate_daily", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_resting_heart_rate_daily", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_resting_heart_rate_daily", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sedentary_period", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_sedentary_period", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_sedentary_period", "start_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sedentary_period", "end_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sedentary_period", "offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_sedentary_period", "end_offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_sedentary_period", "source_date", "DATE", "NO"),
    ("base", "fitbit_sedentary_period", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sedentary_period", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_sedentary_period", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_detail", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_detail", "kind", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_detail", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_detail", "sleep_id", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_detail", "source_date", "DATE", "NO"),
    ("base", "fitbit_sleep_detail", "start_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_detail", "end_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_detail", "offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_sleep_detail", "end_offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_sleep_detail", "category", "VARCHAR", "YES"),
    ("base", "fitbit_sleep_detail", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_detail", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_detail", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_session", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_session", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_session", "source_date", "DATE", "NO"),
    ("base", "fitbit_sleep_session", "start_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_session", "end_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_session", "sleep_minutes", "DOUBLE", "YES"),
    ("base", "fitbit_sleep_session", "offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_sleep_session", "end_offset_seconds", "INTEGER", "YES"),
    ("base", "fitbit_sleep_session", "sleep_type", "VARCHAR", "YES"),
    ("base", "fitbit_sleep_session", "is_main_sleep", "BOOLEAN", "YES"),
    ("base", "fitbit_sleep_session", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_session", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_session", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_session", "minutes_awake", "BIGINT", "YES"),
    ("base", "fitbit_sleep_session", "minutes_in_sleep_period", "BIGINT", "YES"),
    ("base", "fitbit_sleep_session", "minutes_to_fall_asleep", "BIGINT", "YES"),
    ("base", "fitbit_sleep_session", "minutes_after_wake_up", "BIGINT", "YES"),
    ("base", "fitbit_sleep_temperature_daily", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_temperature_daily", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_temperature_daily", "source_date", "DATE", "NO"),
    ("base", "fitbit_sleep_temperature_daily", "nightly_temperature_celsius", "DOUBLE", "NO"),
    ("base", "fitbit_sleep_temperature_daily", "baseline_temperature_celsius", "DOUBLE", "YES"),
    (
        "base",
        "fitbit_sleep_temperature_daily",
        "relative_nightly_stddev_30d_celsius",
        "DOUBLE",
        "YES",
    ),
    ("base", "fitbit_sleep_temperature_daily", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_sleep_temperature_daily", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_sleep_temperature_daily", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_vo2_max_daily", "subject_key", "VARCHAR", "NO"),
    ("base", "fitbit_vo2_max_daily", "record_id", "VARCHAR", "NO"),
    ("base", "fitbit_vo2_max_daily", "source_date", "DATE", "NO"),
    ("base", "fitbit_vo2_max_daily", "vo2_max_ml_kg_min", "DOUBLE", "NO"),
    ("base", "fitbit_vo2_max_daily", "estimated", "BOOLEAN", "YES"),
    ("base", "fitbit_vo2_max_daily", "cardio_fitness_level", "VARCHAR", "YES"),
    ("base", "fitbit_vo2_max_daily", "covariance", "DOUBLE", "YES"),
    ("base", "fitbit_vo2_max_daily", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "fitbit_vo2_max_daily", "source_key", "VARCHAR", "NO"),
    ("base", "fitbit_vo2_max_daily", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "iphone_screen_time_event", "event_key", "VARCHAR", "NO"),
    ("base", "iphone_screen_time_event", "device_key", "VARCHAR", "NO"),
    ("base", "iphone_screen_time_event", "platform", "VARCHAR", "NO"),
    ("base", "iphone_screen_time_event", "source_stream", "VARCHAR", "NO"),
    ("base", "iphone_screen_time_event", "bundle_id", "VARCHAR", "NO"),
    ("base", "iphone_screen_time_event", "event_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "iphone_screen_time_event", "state", "VARCHAR", "NO"),
    ("base", "iphone_screen_time_event", "kind", "UINTEGER", "YES"),
    ("base", "iphone_screen_time_event", "object_key", "VARCHAR", "NO"),
    ("base", "iphone_screen_time_event", "segment_key", "VARCHAR", "NO"),
    ("base", "iphone_screen_time_event", "observed_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "iphone_screen_time_event", "parser_version", "VARCHAR", "NO"),
    ("base", "iphone_screen_time_event", "unknown_field_count", "UINTEGER", "NO"),
    ("base", "iphone_screen_time_event", "duplicate_occurrence_count", "UINTEGER", "NO"),
    ("base", "iphone_screen_time_event", "is_active", "BOOLEAN", "NO"),
    ("base", "iphone_screen_time_event", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "mac_screen_time_event", "event_key", "VARCHAR", "NO"),
    ("base", "mac_screen_time_event", "device_key", "VARCHAR", "NO"),
    ("base", "mac_screen_time_event", "platform", "VARCHAR", "NO"),
    ("base", "mac_screen_time_event", "source_stream", "VARCHAR", "NO"),
    ("base", "mac_screen_time_event", "bundle_id", "VARCHAR", "NO"),
    ("base", "mac_screen_time_event", "event_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "mac_screen_time_event", "state", "VARCHAR", "NO"),
    ("base", "mac_screen_time_event", "kind", "UINTEGER", "YES"),
    ("base", "mac_screen_time_event", "object_key", "VARCHAR", "NO"),
    ("base", "mac_screen_time_event", "segment_key", "VARCHAR", "NO"),
    ("base", "mac_screen_time_event", "observed_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "mac_screen_time_event", "parser_version", "VARCHAR", "NO"),
    ("base", "mac_screen_time_event", "unknown_field_count", "UINTEGER", "NO"),
    ("base", "mac_screen_time_event", "duplicate_occurrence_count", "UINTEGER", "NO"),
    ("base", "mac_screen_time_event", "is_active", "BOOLEAN", "NO"),
    ("base", "mac_screen_time_event", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "owntracks_location", "event_key", "VARCHAR", "NO"),
    ("base", "owntracks_location", "device_key", "VARCHAR", "NO"),
    ("base", "owntracks_location", "event_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "owntracks_location", "latitude", "DOUBLE", "NO"),
    ("base", "owntracks_location", "longitude", "DOUBLE", "NO"),
    ("base", "owntracks_location", "accuracy_m", "DOUBLE", "YES"),
    ("base", "owntracks_location", "velocity_kmh", "DOUBLE", "YES"),
    ("base", "owntracks_location", "object_key", "VARCHAR", "NO"),
    ("base", "owntracks_location", "storage_generation", "BIGINT", "NO"),
    ("base", "owntracks_location", "received_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "owntracks_location", "loaded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("base", "owntracks_location", "parser_version", "VARCHAR", "NO"),
    ("base", "owntracks_location", "motionactivities", "VARCHAR[]", "YES"),
    ("ops", "fitbit_coverage", "subject_key", "VARCHAR", "NO"),
    ("ops", "fitbit_coverage", "data_type", "VARCHAR", "NO"),
    ("ops", "fitbit_coverage", "data_source_family", "VARCHAR", "NO"),
    ("ops", "fitbit_coverage", "aggregation_version", "VARCHAR", "NO"),
    ("ops", "fitbit_coverage", "range_start", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "fitbit_coverage", "range_end", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "fitbit_coverage", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "fitbit_coverage", "source_key", "VARCHAR", "NO"),
    ("ops", "fitbit_coverage", "content_sha256", "VARCHAR", "NO"),
    ("ops", "fitbit_coverage", "source_sha256", "VARCHAR", "NO"),
    ("ops", "fitbit_deleted_record", "subject_key", "VARCHAR", "NO"),
    ("ops", "fitbit_deleted_record", "data_type", "VARCHAR", "NO"),
    ("ops", "fitbit_deleted_record", "record_id", "VARCHAR", "NO"),
    ("ops", "fitbit_deleted_record", "fetched_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "fitbit_deleted_record", "source_key", "VARCHAR", "NO"),
    ("ops", "heartbeat", "monitor_name", "VARCHAR", "NO"),
    ("ops", "heartbeat", "succeeded_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "heartbeat", "run_id", "VARCHAR", "NO"),
    ("ops", "heartbeat", "details", "JSON", "YES"),
    ("ops", "ingestion_metadata", "object_key", "VARCHAR", "NO"),
    ("ops", "ingestion_metadata", "source_id", "VARCHAR", "NO"),
    ("ops", "ingestion_metadata", "schema_version", "UINTEGER", "NO"),
    ("ops", "ingestion_metadata", "subject_key", "VARCHAR", "NO"),
    ("ops", "ingestion_metadata", "source_stream", "VARCHAR", "NO"),
    ("ops", "ingestion_metadata", "logical_key", "VARCHAR", "NO"),
    ("ops", "ingestion_metadata", "observed_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "ingestion_metadata", "content_sha256", "VARCHAR", "NO"),
    ("ops", "ingestion_metadata", "byte_size", "UBIGINT", "NO"),
    ("ops", "ingestion_metadata", "status", "VARCHAR", "NO"),
    ("ops", "ingestion_metadata", "parser_version", "VARCHAR", "YES"),
    ("ops", "ingestion_metadata", "record_count", "UINTEGER", "YES"),
    ("ops", "ingestion_metadata", "started_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "ingestion_metadata", "completed_at", "TIMESTAMP WITH TIME ZONE", "YES"),
    ("ops", "ingestion_metadata", "error_type", "VARCHAR", "YES"),
    ("ops", "ingestion_metadata", "error_message", "VARCHAR", "YES"),
    ("ops", "ingestion_metadata", "retry_count", "UINTEGER", "NO"),
    ("ops", "ingestion_metadata", "storage_created_at", "TIMESTAMP WITH TIME ZONE", "YES"),
    ("ops", "ingestion_metadata", "storage_generation", "UBIGINT", "YES"),
    ("ops", "iphone_screen_time_record", "physical_id", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_record", "device_key", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_record", "source_stream", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_record", "segment_key", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_record", "record_offset", "UBIGINT", "NO"),
    ("ops", "iphone_screen_time_record", "record_metadata_offset", "UBIGINT", "NO"),
    ("ops", "iphone_screen_time_record", "event_key", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_record", "bundle_id", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_record", "event_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "iphone_screen_time_record", "state", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_record", "transition_reason", "VARCHAR", "YES"),
    ("ops", "iphone_screen_time_record", "kind", "UINTEGER", "YES"),
    ("ops", "iphone_screen_time_record", "app_version", "VARCHAR", "YES"),
    ("ops", "iphone_screen_time_record", "app_build", "VARCHAR", "YES"),
    ("ops", "iphone_screen_time_record", "platform_flag", "UINTEGER", "YES"),
    ("ops", "iphone_screen_time_record", "parser_version", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_record", "unknown_field_count", "UINTEGER", "NO"),
    ("ops", "iphone_screen_time_segment", "device_key", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_segment", "source_stream", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_segment", "segment_key", "VARCHAR", "NO"),
    ("ops", "iphone_screen_time_segment", "observed_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "iphone_screen_time_segment", "object_key", "VARCHAR", "NO"),
    ("ops", "job_lock", "job_name", "VARCHAR", "NO"),
    ("ops", "job_lock", "owner_id", "VARCHAR", "NO"),
    ("ops", "job_lock", "expires_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "job_run", "run_id", "VARCHAR", "NO"),
    ("ops", "job_run", "job_name", "VARCHAR", "NO"),
    ("ops", "job_run", "status", "VARCHAR", "NO"),
    ("ops", "job_run", "started_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "job_run", "completed_at", "TIMESTAMP WITH TIME ZONE", "YES"),
    ("ops", "job_run", "details", "JSON", "YES"),
    ("ops", "mac_screen_time_record", "physical_id", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_record", "device_key", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_record", "source_stream", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_record", "segment_key", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_record", "record_offset", "UBIGINT", "NO"),
    ("ops", "mac_screen_time_record", "record_metadata_offset", "UBIGINT", "NO"),
    ("ops", "mac_screen_time_record", "event_key", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_record", "bundle_id", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_record", "event_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "mac_screen_time_record", "state", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_record", "transition_reason", "VARCHAR", "YES"),
    ("ops", "mac_screen_time_record", "kind", "UINTEGER", "YES"),
    ("ops", "mac_screen_time_record", "app_version", "VARCHAR", "YES"),
    ("ops", "mac_screen_time_record", "app_build", "VARCHAR", "YES"),
    ("ops", "mac_screen_time_record", "platform_flag", "UINTEGER", "YES"),
    ("ops", "mac_screen_time_record", "parser_version", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_record", "unknown_field_count", "UINTEGER", "NO"),
    ("ops", "mac_screen_time_segment", "device_key", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_segment", "source_stream", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_segment", "segment_key", "VARCHAR", "NO"),
    ("ops", "mac_screen_time_segment", "observed_at", "TIMESTAMP WITH TIME ZONE", "NO"),
    ("ops", "mac_screen_time_segment", "object_key", "VARCHAR", "NO"),
    ("ops", "schema_migration", "migration_id", "VARCHAR", "NO"),
    ("ops", "schema_migration", "checksum", "VARCHAR", "NO"),
    ("ops", "schema_migration", "applied_at", "TIMESTAMP WITH TIME ZONE", "NO"),
]


LEGACY_KEYS = [
    ("base", "fitbit_activity_interval", "PRIMARY KEY", ["subject_key", "metric", "record_id"]),
    ("base", "fitbit_distance_interval", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_exercise", "PRIMARY KEY", ["subject_key", "record_id"]),
    (
        "base",
        "fitbit_heart_rate_minute",
        "PRIMARY KEY",
        ["subject_key", "data_source_family", "start_at", "aggregation_version"],
    ),
    ("base", "fitbit_heart_rate_zone_interval", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_hrv_daily", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_hrv_daily", "UNIQUE", ["subject_key", "source_date"]),
    ("base", "fitbit_hrv_sample", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_oxygen_saturation_daily", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_oxygen_saturation_daily", "UNIQUE", ["subject_key", "source_date"]),
    ("base", "fitbit_respiratory_rate_daily", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_respiratory_rate_daily", "UNIQUE", ["subject_key", "source_date"]),
    ("base", "fitbit_respiratory_rate_sleep_summary", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_resting_heart_rate_daily", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_sedentary_period", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_sleep_detail", "PRIMARY KEY", ["subject_key", "kind", "record_id"]),
    ("base", "fitbit_sleep_session", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_sleep_temperature_daily", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_sleep_temperature_daily", "UNIQUE", ["subject_key", "source_date"]),
    ("base", "fitbit_vo2_max_daily", "PRIMARY KEY", ["subject_key", "record_id"]),
    ("base", "fitbit_vo2_max_daily", "UNIQUE", ["subject_key", "source_date"]),
    ("base", "iphone_screen_time_event", "PRIMARY KEY", ["event_key"]),
    ("base", "mac_screen_time_event", "PRIMARY KEY", ["event_key"]),
    ("base", "owntracks_location", "PRIMARY KEY", ["event_key"]),
    (
        "ops",
        "fitbit_coverage",
        "PRIMARY KEY",
        ["subject_key", "data_type", "data_source_family", "aggregation_version", "range_start"],
    ),
    ("ops", "fitbit_deleted_record", "PRIMARY KEY", ["subject_key", "data_type", "record_id"]),
    ("ops", "heartbeat", "PRIMARY KEY", ["monitor_name"]),
    ("ops", "ingestion_metadata", "PRIMARY KEY", ["object_key"]),
    ("ops", "iphone_screen_time_record", "PRIMARY KEY", ["physical_id"]),
    (
        "ops",
        "iphone_screen_time_segment",
        "PRIMARY KEY",
        ["device_key", "source_stream", "segment_key"],
    ),
    ("ops", "job_lock", "PRIMARY KEY", ["job_name"]),
    ("ops", "job_run", "PRIMARY KEY", ["run_id"]),
    ("ops", "mac_screen_time_record", "PRIMARY KEY", ["physical_id"]),
    (
        "ops",
        "mac_screen_time_segment",
        "PRIMARY KEY",
        ["device_key", "source_stream", "segment_key"],
    ),
    ("ops", "schema_migration", "PRIMARY KEY", ["migration_id"]),
]


LEGACY_CHECKS = [
    ("base", "fitbit_activity_interval", "(end_at > start_at)"),
    ("base", "fitbit_activity_interval", '(isfinite("value") AND ("value" >= 0))'),
    ("base", "fitbit_activity_interval", "(metric IN ('steps', 'active-zone-minutes'))"),
    ("base", "fitbit_distance_interval", "(end_at > start_at)"),
    ("base", "fitbit_exercise", "(end_at > start_at)"),
    (
        "base",
        "fitbit_heart_rate_minute",
        "((minimum >= 0) AND (minimum <= average) AND (average <= maximum))",
    ),
    ("base", "fitbit_heart_rate_minute", "(date_trunc('minute', start_at) = start_at)"),
    (
        "base",
        "fitbit_heart_rate_minute",
        "(isfinite(average) AND isfinite(minimum) AND isfinite(maximum))",
    ),
    ("base", "fitbit_heart_rate_zone_interval", "(end_at > start_at)"),
    (
        "base",
        "fitbit_resting_heart_rate_daily",
        "(isfinite(beats_per_minute) AND (beats_per_minute >= 0))",
    ),
    ("base", "fitbit_sedentary_period", "(end_at > start_at)"),
    ("base", "fitbit_sleep_detail", "(end_at > start_at)"),
    ("base", "fitbit_sleep_detail", "(kind IN ('sleep-stage', 'sleep-wake'))"),
    (
        "base",
        "fitbit_sleep_session",
        "((sleep_minutes IS NULL) OR (isfinite(sleep_minutes) AND (sleep_minutes >= 0)))",
    ),
    ("base", "fitbit_sleep_session", "(end_at > start_at)"),
    ("base", "iphone_screen_time_event", "(platform = 'ios')"),
    ("base", "iphone_screen_time_event", "(source_stream = 'app-in-focus')"),
    ("base", "iphone_screen_time_event", "(state IN ('start', 'end'))"),
    ("base", "mac_screen_time_event", "(platform = 'macos')"),
    ("base", "mac_screen_time_event", "(source_stream = 'app-usage')"),
    ("base", "mac_screen_time_event", "(state IN ('start', 'end'))"),
    ("base", "owntracks_location", "(latitude BETWEEN -90 AND 90)"),
    ("base", "owntracks_location", "(longitude BETWEEN -180 AND 180)"),
    ("ops", "fitbit_coverage", "(range_end > range_start)"),
    ("ops", "ingestion_metadata", "(status IN ('succeeded', 'failed'))"),
    ("ops", "iphone_screen_time_record", "(source_stream = 'app-in-focus')"),
    ("ops", "iphone_screen_time_segment", "(source_stream = 'app-in-focus')"),
    ("ops", "job_run", "(status IN ('running', 'succeeded', 'failed'))"),
    ("ops", "mac_screen_time_record", "(source_stream = 'app-usage')"),
    ("ops", "mac_screen_time_segment", "(source_stream = 'app-usage')"),
]

LEGACY_DEFAULTS = [("ops", "ingestion_metadata", "retry_count", "0")]


def validate_legacy(connection: Any, *, subject: str) -> None:
    if not subject:
        raise ValueError("configured Fitbit account required")
    actual = connection.execute(
        "SELECT table_schema,table_name,column_name,data_type,is_nullable "
        "FROM information_schema.columns WHERE table_catalog=current_database() "
        "AND table_schema IN ('base','ops') "
        "AND table_name IN (SELECT table_name FROM information_schema.tables "
        "WHERE table_catalog=current_database() AND table_type='BASE TABLE' "
        "AND table_schema IN ('base','ops')) "
        "ORDER BY table_schema,table_name,ordinal_position"
    ).fetchall()
    if actual != LEGACY_COLUMNS:
        raise ValueError("unsupported legacy schema")
    keys = connection.execute(
        "SELECT schema_name,table_name,constraint_type,constraint_column_names "
        "FROM duckdb_constraints() WHERE database_name=current_database() "
        "AND schema_name IN ('base','ops') AND constraint_type IN ('PRIMARY KEY','UNIQUE') "
        "ORDER BY schema_name,table_name,constraint_type,constraint_column_names"
    ).fetchall()
    if keys != LEGACY_KEYS:
        raise ValueError("unsupported legacy schema keys")
    checks = connection.execute(
        "SELECT schema_name,table_name,expression FROM duckdb_constraints() "
        "WHERE database_name=current_database() AND schema_name IN ('base','ops') "
        "AND constraint_type='CHECK' ORDER BY schema_name,table_name,expression"
    ).fetchall()
    defaults = connection.execute(
        "SELECT table_schema,table_name,column_name,column_default FROM information_schema.columns "
        "WHERE table_catalog=current_database() AND table_schema IN ('base','ops') "
        "AND column_default IS NOT NULL ORDER BY table_schema,table_name,column_name"
    ).fetchall()
    if checks != LEGACY_CHECKS or defaults != LEGACY_DEFAULTS:
        raise ValueError("unsupported legacy schema constraints or defaults")
    history = dict(
        connection.execute("SELECT migration_id,checksum FROM ops.schema_migration").fetchall()
    )
    if history != LEGACY_CHECKSUMS:
        raise ValueError("unsupported migration history")
    tables = sorted(
        {row[1] for row in LEGACY_COLUMNS if row[0] == "base" and row[1].startswith("fitbit_")}
    )
    selects = [f'SELECT subject_key FROM base."{table}"' for table in tables]
    selects += [
        "SELECT subject_key FROM ops.fitbit_coverage",
        "SELECT subject_key FROM ops.fitbit_deleted_record",
        "SELECT subject_key FROM ops.ingestion_metadata WHERE source_id='fitbit'",
    ]
    count = connection.execute(
        "SELECT count(*) FROM (" + " UNION ALL ".join(selects) + ") "
        "WHERE subject_key IS DISTINCT FROM ?",
        [subject],
    ).fetchone()[0]
    if count:
        raise ValueError("unexpected Fitbit account")
    for table in (
        "fitbit_resting_heart_rate_daily",
        "fitbit_hrv_daily",
        "fitbit_respiratory_rate_daily",
        "fitbit_oxygen_saturation_daily",
        "fitbit_sleep_temperature_daily",
        "fitbit_vo2_max_daily",
    ):
        duplicate = connection.execute(
            f'SELECT count(*) FROM (SELECT subject_key, source_date FROM base."{table}" '
            "GROUP BY subject_key, source_date HAVING count(*) > 1)"
        ).fetchone()[0]
        if duplicate:
            raise ValueError("duplicate daily Fitbit records")
    invalid_children = connection.execute(
        "SELECT count(*) FROM base.fitbit_sleep_detail d "
        "LEFT JOIN base.fitbit_sleep_session s ON d.subject_key=s.subject_key "
        "AND d.sleep_id=s.record_id "
        "WHERE s.record_id IS NULL OR d.source_date IS DISTINCT FROM s.source_date"
    ).fetchone()[0]
    if invalid_children:
        raise ValueError("sleep detail parent mismatch")
    if connection.execute(
        "SELECT count(*) FROM ops.job_lock WHERE expires_at > current_timestamp"
    ).fetchone()[0]:
        raise ValueError("unexpired writer lease")
    if connection.execute(
        "SELECT count(*) FROM base.fitbit_activity_interval "
        "WHERE metric NOT IN ('steps', 'active-zone-minutes') OR metric IS NULL"
    ).fetchone()[0]:
        raise ValueError("unsupported activity metric")
    if connection.execute(
        "SELECT count(*) FROM base.fitbit_heart_rate_minute WHERE "
        "data_source_family IS DISTINCT FROM 'users/me/dataSourceFamilies/google-wearables' "
        "OR aggregation_version IS DISTINCT FROM 'heart-rate-minute-v1'"
    ).fetchone()[0]:
        raise ValueError("unsupported heart rate series")
    if connection.execute(
        "SELECT count(*) FROM (SELECT sha256(to_json([kind, record_id])) AS new_id "
        "FROM base.fitbit_sleep_detail GROUP BY new_id HAVING new_id IS NULL OR count(*) > 1)"
    ).fetchone()[0]:
        raise ValueError("duplicate sleep detail identifier")


def schema_inventory(db: sqlite3.Connection) -> dict[tuple[str, str, str], str]:
    return {
        (kind, name, table): re.sub(r"\s+", "", sql).replace("IFNOTEXISTS", "")
        for kind, name, table, sql in db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        )
    }


def validate_legacy_sqlite(db: sqlite3.Connection) -> list[str]:
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(LEGACY_SQLITE_SCHEMA)
        expected = schema_inventory(reference)
    actual = schema_inventory(db)
    extras = sorted({table for _, _, table in actual if table not in TABLES})
    if extras:
        raise ValueError(f"unexpected SQLite tables: {extras}")
    runtime = {key: value for key, value in actual.items() if key[2] in TABLES}
    if runtime != expected:
        raise ValueError("SQLite runtime schema differs from the supported collector schema")
    if any(kind not in {"table", "index"} for kind, _, _ in actual):
        raise ValueError("SQLite triggers and views are not supported")
    return extras
