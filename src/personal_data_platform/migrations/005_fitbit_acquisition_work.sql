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
