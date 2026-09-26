-- Keep the applied initial schema intact and resolve platform from the source stream.
CREATE OR REPLACE MACRO ops.screen_time_resolve(keys) AS TABLE (
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
    SELECT event_key, device_key,
           CASE
               WHEN source_stream IN ('app-in-focus', 'App.InFocus') THEN 'ios'
               WHEN source_stream = 'app-usage' THEN 'macos'
               ELSE error('unsupported Screen Time source stream')
           END AS platform,
           source_stream, bundle_id, event_at, state,
           transition_reason, kind, app_version, app_build, platform_flag, object_key, segment_key,
           segment_filename, record_offset, record_metadata_offset, observed_at, parser_version,
           unknown_field_count, greatest(copy_count - 1, 0)::UINTEGER AS duplicate_occurrence_count,
           active AS is_active
    FROM ranked
);
