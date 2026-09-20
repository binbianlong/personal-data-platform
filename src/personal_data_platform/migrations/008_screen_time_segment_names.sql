-- Keep every observed name; one first-arrival name cannot express collisions.
-- NULL means a pre-existing ambiguous segment lost some of its name evidence.
-- Do not guess those names from compacted records or retention-limited Raw.
ALTER TABLE ops.screen_time_segment ADD COLUMN source_segment_names VARCHAR[];
UPDATE ops.screen_time_segment SET source_segment_names = CASE
    WHEN name_ambiguous THEN NULL
    WHEN source_segment_name IS NULL THEN []::VARCHAR[]
    ELSE [source_segment_name] END;

CREATE OR REPLACE MACRO ops.screen_time_matching_record(keys) AS TABLE (
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
  AND NOT EXISTS (
      SELECT 1 FROM ops.screen_time_segment unknown_names
      WHERE unknown_names.device_key = t.device_key
        AND unknown_names.source_stream = t.source_stream
        AND unknown_names.source_segment_names IS NULL
  )
);

-- Repair existing effects atomically, including events outside the conflicting segment.
DELETE FROM ops.screen_time_deletion_match;
INSERT INTO ops.screen_time_deletion_match
SELECT * FROM ops.screen_time_matching_record(
    (SELECT list(physical_id) FROM ops.screen_time_tombstone)
);
UPDATE ops.screen_time_tombstone t SET resolution = CASE
    WHEN NOT is_valid THEN 'invalidated'
    WHEN deletion_reason NOT IN (1, 2) OR deletion_reason IS NULL THEN 'unsupported_reason'
    WHEN NOT EXISTS (SELECT 1 FROM ops.screen_time_deletion_match m
                     WHERE m.tombstone_id = t.physical_id) THEN 'unmatched'
    WHEN deletion_reason = 1 THEN 'ttl_history_retained'
    ELSE 'user_deletion_applied' END;

CREATE TEMP TABLE screen_time_names_expected AS
SELECT *, current_timestamp AS loaded_at FROM ops.screen_time_resolve(
    (SELECT list(DISTINCT event_key) FROM ops.screen_time_record)
);
-- Preserve provenance and loaded_at for events whose analytical result is unchanged.
INSERT OR REPLACE INTO base.screen_time_event
SELECT e.* FROM screen_time_names_expected e
WHERE e.event_key IN (
    SELECT event_key FROM (
        SELECT * EXCLUDE (object_key, segment_key, segment_filename, record_offset,
                          record_metadata_offset, observed_at, loaded_at)
        FROM screen_time_names_expected
        EXCEPT
        SELECT * EXCLUDE (object_key, segment_key, segment_filename, record_offset,
                          record_metadata_offset, observed_at, loaded_at)
        FROM base.screen_time_event
    )
);
DROP TABLE screen_time_names_expected;
