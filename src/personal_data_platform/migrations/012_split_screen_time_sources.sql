-- Split the two canonical collectors while retaining analytical history.
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM base.screen_time_event
    WHERE NOT ((source_stream = 'app-in-focus' AND platform = 'ios')
            OR (source_stream = 'app-usage' AND platform = 'macos'))
       OR NOT starts_with(object_key, 'raw/screen_time/v2/')
    UNION ALL
    SELECT 1 FROM ops.screen_time_segment
    WHERE source_stream NOT IN ('app-in-focus', 'app-usage')
       OR NOT starts_with(object_key, 'raw/screen_time/v2/')
    UNION ALL
    SELECT 1 FROM ops.screen_time_record
    WHERE source_stream NOT IN ('app-in-focus', 'app-usage')
    UNION ALL
    SELECT 1 FROM ops.ingestion_metadata
    WHERE (source_id = 'screen_time' OR starts_with(object_key, 'raw/screen_time/'))
      AND (source_id <> 'screen_time' OR schema_version <> 2
           OR source_stream NOT IN ('app-in-focus', 'app-usage')
           OR NOT starts_with(object_key, 'raw/screen_time/v2/'))
) THEN error('unsupported Screen Time partition or Raw prefix') ELSE true END;

-- DuckDB does not register view-to-table dependencies in duckdb_dependencies().
-- Conservatively reject references outside the known reverse dependency chain.
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM duckdb_views()
    WHERE database_name = current_database() AND NOT internal
      AND regexp_matches(lower(replace(sql, '"', '')),
          '\b(screen_time_event|screen_time_record|screen_time_segment|screen_time_transition|screen_time_interval|daily_screen_time|daily_screen_time_total|fitbit_sleep_screen_time|screen_time_resolve|screen_time_physical_id)\b')
      AND (schema_name, view_name) NOT IN (
          ('marts', 'fitbit_sleep_screen_time'), ('marts', 'daily_screen_time_total'),
          ('marts', 'daily_screen_time'), ('base', 'screen_time_interval'),
          ('base', 'screen_time_transition'))
) THEN error('unexpected dependent Screen Time view') ELSE true END;

SELECT CASE WHEN EXISTS (
    SELECT 1 FROM duckdb_functions()
    WHERE database_name = current_database() AND macro_definition IS NOT NULL
      AND regexp_matches(lower(replace(macro_definition, '"', '')),
          '\b(screen_time_event|screen_time_record|screen_time_segment|screen_time_transition|screen_time_interval|daily_screen_time|daily_screen_time_total|fitbit_sleep_screen_time|screen_time_resolve|screen_time_physical_id)\b')
      AND NOT (schema_name = 'ops' AND function_name IN ('screen_time_resolve', 'screen_time_physical_id'))
) THEN error('unexpected dependent Screen Time macro') ELSE true END;

DROP VIEW IF EXISTS marts.fitbit_sleep_screen_time;
DROP VIEW IF EXISTS marts.daily_screen_time_total;
DROP VIEW IF EXISTS marts.daily_screen_time;
DROP VIEW IF EXISTS base.screen_time_interval;
DROP VIEW IF EXISTS base.screen_time_transition;

CREATE TABLE ops.iphone_screen_time_segment (
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL CHECK (source_stream = 'app-in-focus'),
    segment_key VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    object_key VARCHAR NOT NULL,
    PRIMARY KEY (device_key, source_stream, segment_key)
);
CREATE TABLE ops.iphone_screen_time_record (
    physical_id VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL CHECK (source_stream = 'app-in-focus'),
    segment_key VARCHAR NOT NULL,
    record_offset UBIGINT NOT NULL,
    record_metadata_offset UBIGINT NOT NULL,
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
    unknown_field_count UINTEGER NOT NULL
);
CREATE INDEX iphone_screen_time_record_scope ON ops.iphone_screen_time_record
    (device_key, source_stream, segment_key);
CREATE INDEX iphone_screen_time_record_event ON ops.iphone_screen_time_record (event_key);
CREATE TABLE base.iphone_screen_time_event (
    event_key VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    platform VARCHAR NOT NULL CHECK (platform = 'ios'),
    source_stream VARCHAR NOT NULL CHECK (source_stream = 'app-in-focus'),
    bundle_id VARCHAR NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('start', 'end')),
    kind UINTEGER,
    object_key VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    parser_version VARCHAR NOT NULL,
    unknown_field_count UINTEGER NOT NULL,
    duplicate_occurrence_count UINTEGER NOT NULL,
    is_active BOOLEAN NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL
);
INSERT INTO ops.iphone_screen_time_segment
SELECT device_key, source_stream, segment_key, observed_at,
       'raw/iphone_screen_time/v2/' || substr(object_key, length('raw/screen_time/v2/') + 1)
FROM ops.screen_time_segment WHERE source_stream = 'app-in-focus';
INSERT INTO ops.iphone_screen_time_record
SELECT * FROM ops.screen_time_record WHERE source_stream = 'app-in-focus';
INSERT INTO base.iphone_screen_time_event
SELECT * REPLACE (
    'raw/iphone_screen_time/v2/' || substr(object_key, length('raw/screen_time/v2/') + 1) AS object_key
) FROM base.screen_time_event WHERE source_stream = 'app-in-focus';

UPDATE ops.ingestion_metadata
SET source_id = 'iphone_screen_time',
    object_key = 'raw/iphone_screen_time/v2/' || substr(object_key, length('raw/screen_time/v2/') + 1)
WHERE source_id = 'screen_time' AND source_stream = 'app-in-focus';

CREATE MACRO ops.iphone_screen_time_physical_id(device, stream, segment, record_position, metadata, stamp, digest)
AS sha256(to_json(struct_pack(device := device, stream := stream, segment := segment,
                             record_position := record_position, metadata := metadata,
                             stamp := stamp, digest := digest)));
CREATE MACRO ops.iphone_screen_time_resolve(keys) AS TABLE (
    WITH records AS (
        SELECT r.*, s.observed_at, s.object_key
        FROM ops.iphone_screen_time_record r
        JOIN ops.iphone_screen_time_segment s USING (device_key, source_stream, segment_key)
        WHERE r.event_key IN (SELECT unnest(keys))
    ), copies AS (
        SELECT * FROM records
        QUALIFY row_number() OVER (
            PARTITION BY device_key, source_stream, segment_key, record_offset, event_key
            ORDER BY observed_at DESC, object_key DESC, record_metadata_offset DESC
        ) = 1
    ), ranked AS (
        SELECT *, count(*) OVER (PARTITION BY event_key) AS copy_count
        FROM copies
        QUALIFY row_number() OVER (
            PARTITION BY event_key
            ORDER BY observed_at DESC, object_key DESC, record_metadata_offset DESC
        ) = 1
    )
    SELECT event_key, device_key, 'ios' AS platform,
           source_stream, bundle_id, event_at, state, kind, object_key, segment_key,
           observed_at, parser_version, unknown_field_count,
           greatest(copy_count - 1, 0)::UINTEGER AS duplicate_occurrence_count,
           true AS is_active
    FROM ranked
);

UPDATE ops.heartbeat SET monitor_name = 'iphone_screen_time_reconciliation',
    details = CASE WHEN details IS NULL THEN NULL ELSE
        json_merge_patch(details, '{"source_id":"iphone_screen_time"}') END
WHERE monitor_name = 'screen_time_reconciliation';

CREATE TABLE ops.mac_screen_time_segment (
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL CHECK (source_stream = 'app-usage'),
    segment_key VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    object_key VARCHAR NOT NULL,
    PRIMARY KEY (device_key, source_stream, segment_key)
);
CREATE TABLE ops.mac_screen_time_record (
    physical_id VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL CHECK (source_stream = 'app-usage'),
    segment_key VARCHAR NOT NULL,
    record_offset UBIGINT NOT NULL,
    record_metadata_offset UBIGINT NOT NULL,
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
    unknown_field_count UINTEGER NOT NULL
);
CREATE INDEX mac_screen_time_record_scope ON ops.mac_screen_time_record
    (device_key, source_stream, segment_key);
CREATE INDEX mac_screen_time_record_event ON ops.mac_screen_time_record (event_key);
CREATE TABLE base.mac_screen_time_event (
    event_key VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    platform VARCHAR NOT NULL CHECK (platform = 'macos'),
    source_stream VARCHAR NOT NULL CHECK (source_stream = 'app-usage'),
    bundle_id VARCHAR NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('start', 'end')),
    kind UINTEGER,
    object_key VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    parser_version VARCHAR NOT NULL,
    unknown_field_count UINTEGER NOT NULL,
    duplicate_occurrence_count UINTEGER NOT NULL,
    is_active BOOLEAN NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL
);
INSERT INTO ops.mac_screen_time_segment
SELECT device_key, source_stream, segment_key, observed_at,
       'raw/mac_screen_time/v2/' || substr(object_key, length('raw/screen_time/v2/') + 1)
FROM ops.screen_time_segment WHERE source_stream = 'app-usage';
INSERT INTO ops.mac_screen_time_record
SELECT * FROM ops.screen_time_record WHERE source_stream = 'app-usage';
INSERT INTO base.mac_screen_time_event
SELECT * REPLACE (
    'raw/mac_screen_time/v2/' || substr(object_key, length('raw/screen_time/v2/') + 1) AS object_key
) FROM base.screen_time_event WHERE source_stream = 'app-usage';

UPDATE ops.ingestion_metadata
SET source_id = 'mac_screen_time',
    object_key = 'raw/mac_screen_time/v2/' || substr(object_key, length('raw/screen_time/v2/') + 1)
WHERE source_id = 'screen_time' AND source_stream = 'app-usage';

CREATE MACRO ops.mac_screen_time_physical_id(device, stream, segment, record_position, metadata, stamp, digest)
AS sha256(to_json(struct_pack(device := device, stream := stream, segment := segment,
                             record_position := record_position, metadata := metadata,
                             stamp := stamp, digest := digest)));
CREATE MACRO ops.mac_screen_time_resolve(keys) AS TABLE (
    WITH records AS (
        SELECT r.*, s.observed_at, s.object_key
        FROM ops.mac_screen_time_record r
        JOIN ops.mac_screen_time_segment s USING (device_key, source_stream, segment_key)
        WHERE r.event_key IN (SELECT unnest(keys))
    ), copies AS (
        SELECT * FROM records
        QUALIFY row_number() OVER (
            PARTITION BY device_key, source_stream, segment_key, record_offset, event_key
            ORDER BY observed_at DESC, object_key DESC, record_metadata_offset DESC
        ) = 1
    ), ranked AS (
        SELECT *, count(*) OVER (PARTITION BY event_key) AS copy_count
        FROM copies
        QUALIFY row_number() OVER (
            PARTITION BY event_key
            ORDER BY observed_at DESC, object_key DESC, record_metadata_offset DESC
        ) = 1
    )
    SELECT event_key, device_key, 'macos' AS platform,
           source_stream, bundle_id, event_at, state, kind, object_key, segment_key,
           observed_at, parser_version, unknown_field_count,
           greatest(copy_count - 1, 0)::UINTEGER AS duplicate_occurrence_count,
           true AS is_active
    FROM ranked
);

UPDATE ops.heartbeat SET monitor_name = 'mac_screen_time_reconciliation',
    details = CASE WHEN details IS NULL THEN NULL ELSE
        json_merge_patch(details, '{"source_id":"mac_screen_time"}') END
WHERE monitor_name = 'screen_time_app_usage_reconciliation';

DROP MACRO ops.screen_time_resolve;
DROP MACRO ops.screen_time_physical_id;
DROP TABLE ops.screen_time_record;
DROP TABLE ops.screen_time_segment;
DROP TABLE base.screen_time_event;
