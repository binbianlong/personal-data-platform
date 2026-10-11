-- Keep only the current physical Screen Time records. Analytical history stays in base.

DROP MACRO ops.screen_time_resolve;
DROP MACRO ops.screen_time_matching_record;

CREATE TABLE ops.screen_time_record_next (
    physical_id VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
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
    unknown_field_count UINTEGER NOT NULL,
    segment_filename VARCHAR NOT NULL
);

INSERT INTO ops.screen_time_record_next
SELECT physical_id, device_key, source_stream, segment_key, record_offset,
       record_metadata_offset, event_key, bundle_id, event_at, state, transition_reason,
       kind, app_version, app_build, platform_flag, parser_version,
       unknown_field_count, segment_filename
FROM ops.screen_time_record
WHERE is_current AND is_valid;

DROP TABLE ops.screen_time_deletion_match;
DROP TABLE ops.screen_time_tombstone;
DROP TABLE ops.screen_time_record;
ALTER TABLE ops.screen_time_record_next RENAME TO screen_time_record;
CREATE INDEX screen_time_record_scope ON ops.screen_time_record
    (device_key, source_stream, segment_key);
CREATE INDEX screen_time_record_event ON ops.screen_time_record (event_key);

ALTER TABLE ops.screen_time_segment DROP COLUMN source_segment_name;
ALTER TABLE ops.screen_time_segment DROP COLUMN name_ambiguous;
ALTER TABLE ops.screen_time_segment DROP COLUMN source_segment_names;

CREATE MACRO ops.screen_time_resolve(keys) AS TABLE (
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
           source_stream, bundle_id, event_at, state, transition_reason, kind,
           app_version, app_build, platform_flag, object_key, segment_key,
           segment_filename, record_offset, record_metadata_offset, observed_at,
           parser_version, unknown_field_count,
           greatest(copy_count - 1, 0)::UINTEGER AS duplicate_occurrence_count,
           true AS is_active
    FROM ranked
);

UPDATE base.screen_time_event e SET is_active = false, loaded_at = current_timestamp
WHERE e.is_active AND NOT EXISTS (
    SELECT 1 FROM ops.screen_time_record r WHERE r.event_key = e.event_key
);

INSERT INTO base.screen_time_event
SELECT *, current_timestamp FROM ops.screen_time_resolve(
    (SELECT list(DISTINCT event_key) FROM ops.screen_time_record)
)
ON CONFLICT (event_key) DO UPDATE SET
    device_key = excluded.device_key,
    platform = excluded.platform,
    source_stream = excluded.source_stream,
    bundle_id = excluded.bundle_id,
    event_at = excluded.event_at,
    state = excluded.state,
    transition_reason = excluded.transition_reason,
    kind = excluded.kind,
    app_version = excluded.app_version,
    app_build = excluded.app_build,
    platform_flag = excluded.platform_flag,
    object_key = excluded.object_key,
    segment_key = excluded.segment_key,
    segment_filename = excluded.segment_filename,
    record_offset = excluded.record_offset,
    record_metadata_offset = excluded.record_metadata_offset,
    observed_at = excluded.observed_at,
    parser_version = excluded.parser_version,
    unknown_field_count = excluded.unknown_field_count,
    duplicate_occurrence_count = excluded.duplicate_occurrence_count,
    is_active = excluded.is_active,
    loaded_at = excluded.loaded_at
WHERE base.screen_time_event.device_key IS DISTINCT FROM excluded.device_key
   OR base.screen_time_event.platform IS DISTINCT FROM excluded.platform
   OR base.screen_time_event.source_stream IS DISTINCT FROM excluded.source_stream
   OR base.screen_time_event.bundle_id IS DISTINCT FROM excluded.bundle_id
   OR base.screen_time_event.event_at IS DISTINCT FROM excluded.event_at
   OR base.screen_time_event.state IS DISTINCT FROM excluded.state
   OR base.screen_time_event.transition_reason IS DISTINCT FROM excluded.transition_reason
   OR base.screen_time_event.kind IS DISTINCT FROM excluded.kind
   OR base.screen_time_event.app_version IS DISTINCT FROM excluded.app_version
   OR base.screen_time_event.app_build IS DISTINCT FROM excluded.app_build
   OR base.screen_time_event.platform_flag IS DISTINCT FROM excluded.platform_flag
   OR base.screen_time_event.parser_version IS DISTINCT FROM excluded.parser_version
   OR base.screen_time_event.unknown_field_count IS DISTINCT FROM excluded.unknown_field_count
   OR base.screen_time_event.duplicate_occurrence_count IS DISTINCT FROM excluded.duplicate_occurrence_count
   OR base.screen_time_event.is_active IS DISTINCT FROM excluded.is_active;

-- Fitbit's pre-push Raw receipts no longer protect ordering or recovery.
DELETE FROM ops.ingestion_metadata WHERE source_id = 'fitbit';

DELETE FROM ops.reconciliation_run
WHERE coalesce(completed_at, started_at) < current_timestamp - INTERVAL 7 DAY;
DELETE FROM ops.job_run
WHERE coalesce(completed_at, started_at) < current_timestamp - INTERVAL 7 DAY
  AND run_id <> coalesce((
      SELECT run_id FROM ops.job_run
      WHERE job_name = 'daily-reconciliation-west' AND status = 'succeeded'
        AND json_extract_string(details, '$.last_completed_target_date') IS NOT NULL
      ORDER BY completed_at DESC LIMIT 1
  ), '');
