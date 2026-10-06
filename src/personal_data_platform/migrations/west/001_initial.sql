CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS base;
CREATE SCHEMA IF NOT EXISTS marts;

CREATE TABLE IF NOT EXISTS ops.ingestion_metadata (
    object_key VARCHAR PRIMARY KEY,
    source_id VARCHAR NOT NULL,
    schema_version UINTEGER NOT NULL,
    subject_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    logical_key VARCHAR NOT NULL,
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
    retry_count UINTEGER NOT NULL DEFAULT 0,
    storage_created_at TIMESTAMPTZ,
    storage_generation UBIGINT,
    retention_expired_at TIMESTAMPTZ
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

CREATE TABLE base.screen_time_event (
    event_key VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    platform VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    bundle_id VARCHAR NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('start', 'end')),
    transition_reason VARCHAR,
    kind UINTEGER,
    app_version VARCHAR,
    app_build VARCHAR,
    platform_flag UINTEGER,
    object_key VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    segment_filename VARCHAR NOT NULL,
    record_offset UBIGINT NOT NULL,
    record_metadata_offset UBIGINT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    parser_version VARCHAR NOT NULL,
    unknown_field_count UINTEGER NOT NULL,
    duplicate_occurrence_count UINTEGER NOT NULL,
    is_active BOOLEAN NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE ops.screen_time_segment (
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    object_key VARCHAR NOT NULL,
    source_segment_name VARCHAR,
    name_ambiguous BOOLEAN NOT NULL DEFAULT false,
    source_segment_names VARCHAR[] NOT NULL DEFAULT [],
    PRIMARY KEY (device_key, source_stream, segment_key)
);

-- Decoded fields permit choosing another physical copy after a correction.
-- Original protobuf/SEGB bytes remain exclusively in Raw.
CREATE TABLE ops.screen_time_record (
    physical_id VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    record_offset UBIGINT NOT NULL,
    record_metadata_offset UBIGINT NOT NULL,
    payload_length UINTEGER NOT NULL,
    record_timestamp_cocoa DOUBLE,
    event_key VARCHAR NOT NULL,
    bundle_id VARCHAR NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    state VARCHAR NOT NULL,
    transition_reason VARCHAR,
    kind UINTEGER,
    app_version VARCHAR,
    app_build VARCHAR,
    platform_flag UINTEGER,
    parser_version VARCHAR NOT NULL,
    unknown_field_count UINTEGER NOT NULL,
    segment_filename VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    object_key VARCHAR NOT NULL,
    is_current BOOLEAN NOT NULL,
    is_valid BOOLEAN NOT NULL
);
CREATE INDEX screen_time_record_scope ON ops.screen_time_record
    (device_key, source_stream, segment_key);
CREATE INDEX screen_time_record_event ON ops.screen_time_record (event_key);

CREATE TABLE ops.screen_time_tombstone (
    physical_id VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    target_segment_name VARCHAR,
    target_offset UBIGINT,
    target_length UINTEGER,
    target_event_timestamp DOUBLE,
    deletion_reason UINTEGER,
    observed_at TIMESTAMPTZ NOT NULL,
    object_key VARCHAR NOT NULL,
    is_valid BOOLEAN NOT NULL,
    resolution VARCHAR NOT NULL
);
CREATE INDEX screen_time_tombstone_target ON ops.screen_time_tombstone
    (device_key, source_stream, target_segment_name);
CREATE INDEX screen_time_tombstone_scope ON ops.screen_time_tombstone
    (device_key, source_stream, segment_key);
CREATE TABLE ops.screen_time_deletion_match (
    tombstone_id VARCHAR NOT NULL,
    physical_id VARCHAR NOT NULL,
    PRIMARY KEY (tombstone_id, physical_id)
);
CREATE INDEX screen_time_match_record ON ops.screen_time_deletion_match (physical_id);

-- The digest identifies content at a physical position, independent of observation
-- count and parser version. Parser corrections replace the normalized fields.
CREATE MACRO ops.screen_time_physical_id(device, stream, segment, record_position, metadata, stamp, digest)
AS sha256(to_json(struct_pack(device := device, stream := stream, segment := segment,
                             record_position := record_position, metadata := metadata,
                             stamp := stamp, digest := digest)));

-- Match deletions only when the observed name uniquely identifies a segment.
CREATE MACRO ops.screen_time_matching_record(keys) AS TABLE (
WITH tombstones AS (
    SELECT * FROM ops.screen_time_tombstone WHERE physical_id IN (SELECT unnest(keys))
), targets AS (
    SELECT DISTINCT device_key, source_stream, target_segment_name FROM tombstones
), names AS (
    SELECT s.device_key, s.source_stream, alias.source_segment_name,
           min(s.segment_key) AS segment_key
    FROM ops.screen_time_segment s
    CROSS JOIN unnest(s.source_segment_names) AS alias(source_segment_name)
    JOIN targets t ON s.device_key = t.device_key AND s.source_stream = t.source_stream
        AND alias.source_segment_name = t.target_segment_name
    GROUP BY s.device_key, s.source_stream, alias.source_segment_name
    HAVING count(*) = 1 AND NOT bool_or(name_ambiguous)
)
SELECT t.physical_id AS tombstone_id, r.physical_id
FROM tombstones t
JOIN names n ON t.device_key = n.device_key AND t.source_stream = n.source_stream
    AND t.target_segment_name = n.source_segment_name
JOIN ops.screen_time_record r ON r.device_key = n.device_key AND r.source_stream = n.source_stream
    AND r.segment_key = n.segment_key AND r.record_metadata_offset = t.target_offset
    AND r.payload_length = t.target_length
    AND abs(r.record_timestamp_cocoa - t.target_event_timestamp) <= 0.000001
WHERE t.is_valid AND r.is_valid AND t.deletion_reason IN (1, 2)
);

-- Restrict keys before any ranking/aggregation, including when invoked during a load.
CREATE MACRO ops.screen_time_resolve(keys) AS TABLE (
    WITH records AS (
        SELECT * FROM ops.screen_time_record WHERE event_key IN (SELECT unnest(keys))
    ), effects AS (
        SELECT r.physical_id, bool_or(t.deletion_reason = 1) AS ttl,
               bool_or(t.deletion_reason = 2) AS deleted
        FROM records r
        JOIN ops.screen_time_deletion_match m ON m.physical_id = r.physical_id
        JOIN ops.screen_time_tombstone t ON t.physical_id = m.tombstone_id
        GROUP BY r.physical_id
    ), eligible AS (
        SELECT r.*, r.is_valid AND (r.is_current OR coalesce(e.ttl, false))
            AND NOT bool_or(coalesce(e.deleted, false)) OVER (PARTITION BY r.event_key) AS active
        FROM records r LEFT JOIN effects e USING (physical_id)
    ), copies AS (
        SELECT * FROM eligible
        QUALIFY row_number() OVER (
            PARTITION BY device_key, source_stream, segment_key, record_offset, event_key
            ORDER BY active DESC, observed_at DESC, object_key DESC, record_metadata_offset DESC
        ) = 1
    ), ranked AS (
        SELECT *, count(*) FILTER (WHERE active) OVER (PARTITION BY event_key) AS copy_count
        FROM copies
        QUALIFY row_number() OVER (
            PARTITION BY event_key
            ORDER BY active DESC, observed_at DESC, object_key DESC, record_metadata_offset DESC
        ) = 1
    )
    SELECT event_key, device_key, 'ios' AS platform, source_stream, bundle_id, event_at, state,
           transition_reason, kind, app_version, app_build, platform_flag, object_key, segment_key,
           segment_filename, record_offset, record_metadata_offset, observed_at, parser_version,
           unknown_field_count, greatest(copy_count - 1, 0)::UINTEGER AS duplicate_occurrence_count,
           active AS is_active
    FROM ranked
);

CREATE VIEW base.screen_time_transition AS
SELECT * EXCLUDE (is_active, loaded_at) FROM base.screen_time_event WHERE is_active;
