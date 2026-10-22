-- Keep physical-copy diagnostics in ops and expose only the retained event fields.
ALTER TABLE base.screen_time_event DROP COLUMN app_version;
ALTER TABLE base.screen_time_event DROP COLUMN app_build;
ALTER TABLE base.screen_time_event DROP COLUMN transition_reason;
ALTER TABLE base.screen_time_event DROP COLUMN platform_flag;
ALTER TABLE base.screen_time_event DROP COLUMN segment_filename;
ALTER TABLE base.screen_time_event DROP COLUMN record_offset;
ALTER TABLE base.screen_time_event DROP COLUMN record_metadata_offset;

CREATE OR REPLACE MACRO ops.screen_time_resolve(keys) AS TABLE (
    WITH records AS (
        SELECT r.*, s.observed_at, s.object_key
        FROM ops.screen_time_record r
        JOIN ops.screen_time_segment s USING (device_key, source_stream, segment_key)
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
    SELECT event_key, device_key,
           CASE
               WHEN source_stream IN ('app-in-focus', 'App.InFocus') THEN 'ios'
               WHEN source_stream = 'app-usage' THEN 'macos'
               ELSE error('unsupported Screen Time source stream')
           END AS platform,
           source_stream, bundle_id, event_at, state, kind, object_key, segment_key,
           observed_at, parser_version, unknown_field_count,
           greatest(copy_count - 1, 0)::UINTEGER AS duplicate_occurrence_count,
           true AS is_active
    FROM ranked
);

CREATE OR REPLACE VIEW base.screen_time_transition AS
SELECT * EXCLUDE (is_active, loaded_at) FROM base.screen_time_event WHERE is_active;
