-- Fitbit metrics and acquisition coverage; all times are UTC.

CREATE TABLE IF NOT EXISTS base.fitbit_steps (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_resting_heart_rate (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_active_zone (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_sleep (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_sleep_stage (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_sleep_wake (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);


CREATE TABLE IF NOT EXISTS ops.fitbit_coverage (
    subject_key VARCHAR NOT NULL,
    data_type VARCHAR NOT NULL,
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    origin VARCHAR NOT NULL,
    source_key VARCHAR NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    source_sha256 VARCHAR DEFAULT '',
    PRIMARY KEY (subject_key, data_type, range_start),
    CHECK (range_end > range_start)
);

-- Keep ordering after an ID moves between ranges and is later deleted.
CREATE TABLE IF NOT EXISTS ops.fitbit_deleted_record (
    subject_key VARCHAR NOT NULL,
    data_type VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    origin VARCHAR NOT NULL,
    source_key VARCHAR NOT NULL,
    PRIMARY KEY (subject_key, data_type, record_id)
);

CREATE TABLE ops.fitbit_scope (
    scope_key VARCHAR PRIMARY KEY,
    subject_key VARCHAR NOT NULL,
    data_type VARCHAR NOT NULL,
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    aggregation_version VARCHAR NOT NULL,
    CHECK (range_end > range_start)
);
CREATE TABLE ops.fitbit_notification (
    notification_id VARCHAR PRIMARY KEY,
    subject_key VARCHAR NOT NULL,
    received_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE ops.fitbit_notification_scope (
    notification_id VARCHAR NOT NULL,
    scope_key VARCHAR NOT NULL,
    attempt_id VARCHAR,
    PRIMARY KEY (notification_id, scope_key)
);
CREATE TABLE ops.fitbit_attempt (
    attempt_id VARCHAR PRIMARY KEY,
    scope_key VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL CHECK (status IN ('pending', 'succeeded')),
    source_sha256 VARCHAR,
    raw_keys VARCHAR[],
    completed_at TIMESTAMPTZ
);
CREATE TABLE ops.fitbit_scope_success (
    scope_key VARCHAR PRIMARY KEY,
    attempt_id VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    raw_keys VARCHAR[] NOT NULL
);
CREATE TABLE ops.fitbit_bundle (
    bundle_id VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL CHECK (status IN ('pending', 'succeeded', 'superseded'))
);
CREATE TABLE ops.fitbit_bundle_attempt (
    bundle_id VARCHAR NOT NULL,
    attempt_id VARCHAR NOT NULL,
    PRIMARY KEY (bundle_id, attempt_id)
);
CREATE TABLE ops.fitbit_bundle_chunk (
    bundle_id VARCHAR NOT NULL,
    chunk_index INTEGER NOT NULL,
    raw_key VARCHAR NOT NULL UNIQUE,
    compressed_sha256 VARCHAR NOT NULL,
    compressed_size BIGINT NOT NULL CHECK (compressed_size > 0 AND compressed_size <= 16777216),
    storage_generation BIGINT,
    PRIMARY KEY (bundle_id, chunk_index)
);
CREATE TABLE ops.fitbit_repair_cursor (
    cursor_id VARCHAR PRIMARY KEY,
    subject_key VARCHAR NOT NULL,
    next_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    data_types VARCHAR[] NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (next_start <= range_end)
);
CREATE TABLE ops.fitbit_device_sync (
    subject_key VARCHAR NOT NULL,
    device_id VARCHAR NOT NULL,
    last_sync_time VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, device_id)
);

-- Complete UTC minute windows from the Google wearables heart rate rollup.
CREATE TABLE IF NOT EXISTS base.fitbit_heart_rate_minute (
    subject_key VARCHAR NOT NULL,
    data_source_family VARCHAR NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    aggregation_version VARCHAR NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    average DOUBLE NOT NULL,
    minimum DOUBLE NOT NULL,
    maximum DOUBLE NOT NULL,
    sample_count BIGINT,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, data_source_family, start_at, aggregation_version),
    CHECK (end_at = start_at + INTERVAL '1 minute'),
    CHECK (date_trunc('minute', start_at) = start_at),
    CHECK (isfinite(average) AND isfinite(minimum) AND isfinite(maximum)),
    CHECK (minimum >= 0 AND minimum <= average AND average <= maximum),
    CHECK (sample_count IS NULL OR sample_count > 0)
);

CREATE TABLE IF NOT EXISTS ops.fitbit_minute_coverage (
    subject_key VARCHAR NOT NULL,
    data_source_family VARCHAR NOT NULL,
    aggregation_version VARCHAR NOT NULL,
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    origin VARCHAR NOT NULL,
    source_key VARCHAR NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    PRIMARY KEY (subject_key, data_source_family, aggregation_version, range_start),
    CHECK (range_end > range_start)
);
