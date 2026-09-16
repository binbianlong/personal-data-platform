-- Verify the 006 bootstrap before retiring the legacy analytical path.
-- These full-history checks run once, inside the migration transaction.
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM base.screen_time_segment_observation old
    LEFT JOIN ops.screen_time_segment current
        USING (device_key, source_stream, segment_key)
    WHERE current.segment_key IS NULL
       OR (current.observed_at, current.object_key) < (old.observed_at, old.object_key)
) THEN error('Screen Time cutover blocked: legacy segment observations are not migrated') END;

-- Compare physical identities, allowing later parser corrections and invalidations.
-- Only the last metadata entry at each position was eligible for the 006 bootstrap.
CREATE TEMP TABLE screen_time_cutover_legacy AS
SELECT *, ops.screen_time_physical_id(
    device_key, source_stream, segment_key, record_offset, record_metadata_offset,
    record_timestamp_cocoa, sha256(original_payload)
) AS physical_id
FROM base.screen_time_record_occurrence
QUALIFY row_number() OVER (
    PARTITION BY object_key, record_offset ORDER BY record_metadata_offset DESC
) = 1;

SELECT CASE WHEN EXISTS (
    SELECT 1 FROM screen_time_cutover_legacy old
    WHERE upper(record_state) = 'WRITTEN' AND crc_passed IS DISTINCT FROM false
      AND (
        (record_kind = 'event' AND event_key IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM ops.screen_time_record r WHERE r.physical_id = old.physical_id
        )) OR
        (record_kind = 'tombstone' AND NOT EXISTS (
            SELECT 1 FROM ops.screen_time_tombstone t WHERE t.physical_id = old.physical_id
        ))
      )
) THEN error('Screen Time cutover blocked: legacy physical records are not migrated') END;
DROP TABLE screen_time_cutover_legacy;

-- Reuse ingestion rules for validation; do not reproduce deletion/TTL rules here.
CREATE TEMP TABLE screen_time_cutover_matches AS
SELECT * FROM ops.screen_time_matching_record(
    (SELECT list(physical_id) FROM ops.screen_time_tombstone)
);
SELECT CASE WHEN EXISTS (
    (SELECT * FROM screen_time_cutover_matches EXCEPT SELECT * FROM ops.screen_time_deletion_match)
    UNION ALL
    (SELECT * FROM ops.screen_time_deletion_match EXCEPT SELECT * FROM screen_time_cutover_matches)
) THEN error('Screen Time cutover blocked: deletion matches differ from ingestion state') END;
DROP TABLE screen_time_cutover_matches;

-- Same-content observations intentionally keep event provenance unchanged.
-- Compare analytical fields and active state, not observation provenance.
CREATE TEMP TABLE screen_time_cutover_expected AS
SELECT * EXCLUDE (object_key, segment_key, segment_filename, record_offset,
                  record_metadata_offset, observed_at)
FROM ops.screen_time_resolve((SELECT list(DISTINCT event_key) FROM ops.screen_time_record));
CREATE TEMP TABLE screen_time_cutover_actual AS
SELECT * EXCLUDE (object_key, segment_key, segment_filename, record_offset,
                  record_metadata_offset, observed_at, loaded_at)
FROM base.screen_time_event
WHERE is_active OR event_key IN (SELECT event_key FROM ops.screen_time_record);
SELECT CASE WHEN EXISTS (
    (SELECT * FROM screen_time_cutover_expected EXCEPT SELECT * FROM screen_time_cutover_actual)
    UNION ALL
    (SELECT * FROM screen_time_cutover_actual EXCEPT SELECT * FROM screen_time_cutover_expected)
) THEN error('Screen Time cutover blocked: analytical events differ from ingestion state') END;
DROP TABLE screen_time_cutover_expected;
DROP TABLE screen_time_cutover_actual;

-- Replace the entry view first so existing downstream views remain queryable.
CREATE OR REPLACE VIEW base.screen_time_transition AS
SELECT * EXCLUDE (is_active, loaded_at) FROM base.screen_time_event WHERE is_active;
DROP VIEW IF EXISTS base.screen_time_legacy_transition;
DROP VIEW IF EXISTS base.screen_time_tombstone_status;
DROP VIEW IF EXISTS base.screen_time_tombstone_match;
