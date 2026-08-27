CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS base;
CREATE SCHEMA IF NOT EXISTS marts;

CREATE TABLE IF NOT EXISTS ops.ingestion_metadata (
    object_key VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    byte_size UBIGINT NOT NULL,
    status VARCHAR NOT NULL CHECK (status IN ('loading', 'succeeded', 'failed')),
    parser_version VARCHAR,
    record_count UINTEGER,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    error_type VARCHAR,
    error_message VARCHAR,
    retry_count UINTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ops.job_run (
    run_id VARCHAR PRIMARY KEY,
    job_name VARCHAR NOT NULL,
    status VARCHAR NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    details JSON
);

CREATE TABLE IF NOT EXISTS ops.job_lock (
    job_name VARCHAR PRIMARY KEY,
    owner_id VARCHAR NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.reconciliation_run (
    run_id VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    raw_object_count UBIGINT,
    loaded_object_count UBIGINT,
    missing_object_count UBIGINT,
    failed_object_count UBIGINT,
    details JSON
);

CREATE TABLE IF NOT EXISTS ops.heartbeat (
    monitor_name VARCHAR PRIMARY KEY,
    succeeded_at TIMESTAMPTZ NOT NULL,
    run_id VARCHAR NOT NULL,
    details JSON
);

CREATE TABLE IF NOT EXISTS base.screen_time_segment_observation (
    object_key VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    byte_size UBIGINT NOT NULL,
    record_count UINTEGER NOT NULL,
    parser_version VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS base.screen_time_record_occurrence (
    object_key VARCHAR NOT NULL,
    record_offset UBIGINT NOT NULL,
    record_metadata_offset UBIGINT NOT NULL,
    event_key VARCHAR NOT NULL,
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    segment_sha256 VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    segment_filename VARCHAR NOT NULL,
    record_state VARCHAR NOT NULL,
    segment_record_timestamp TIMESTAMPTZ,
    crc_passed BOOLEAN,
    transition_reason VARCHAR,
    kind UINTEGER,
    in_foreground BOOLEAN NOT NULL,
    cf_absolute_time DOUBLE NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    bundle_id VARCHAR NOT NULL,
    app_version VARCHAR,
    app_build VARCHAR,
    platform_flag UINTEGER,
    unknown_field_count UINTEGER NOT NULL,
    original_payload BLOB NOT NULL,
    parser_version VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (object_key, record_metadata_offset)
);

CREATE INDEX IF NOT EXISTS screen_time_occurrence_event_key_idx
    ON base.screen_time_record_occurrence (event_key);
CREATE INDEX IF NOT EXISTS screen_time_occurrence_event_at_idx
    ON base.screen_time_record_occurrence (event_at);
