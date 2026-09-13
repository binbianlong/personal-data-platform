DROP INDEX IF EXISTS base.screen_time_occurrence_event_key_idx;
DROP INDEX IF EXISTS base.screen_time_occurrence_event_at_idx;
ALTER TABLE base.screen_time_record_occurrence ALTER COLUMN event_key DROP NOT NULL;
ALTER TABLE base.screen_time_record_occurrence ALTER COLUMN in_foreground DROP NOT NULL;
ALTER TABLE base.screen_time_record_occurrence ALTER COLUMN cf_absolute_time DROP NOT NULL;
ALTER TABLE base.screen_time_record_occurrence ALTER COLUMN event_at DROP NOT NULL;
ALTER TABLE base.screen_time_record_occurrence ALTER COLUMN bundle_id DROP NOT NULL;
ALTER TABLE base.screen_time_record_occurrence ADD COLUMN record_kind VARCHAR DEFAULT 'event';
ALTER TABLE base.screen_time_record_occurrence ADD COLUMN payload_length UINTEGER;
ALTER TABLE base.screen_time_record_occurrence ADD COLUMN record_timestamp_cocoa DOUBLE;
ALTER TABLE base.screen_time_record_occurrence ADD COLUMN target_segment_name VARCHAR;
ALTER TABLE base.screen_time_record_occurrence ADD COLUMN target_offset UBIGINT;
ALTER TABLE base.screen_time_record_occurrence ADD COLUMN target_length UINTEGER;
ALTER TABLE base.screen_time_record_occurrence ADD COLUMN target_event_timestamp DOUBLE;
ALTER TABLE base.screen_time_record_occurrence ADD COLUMN deletion_reason UINTEGER;
UPDATE base.screen_time_record_occurrence
SET payload_length = octet_length(original_payload),
    record_timestamp_cocoa = epoch(segment_record_timestamp) - 978307200;
ALTER TABLE base.screen_time_segment_observation ADD COLUMN source_segment_name VARCHAR;
ALTER TABLE base.screen_time_segment_observation ADD COLUMN segment_kind VARCHAR;

CREATE INDEX screen_time_occurrence_event_key_idx ON base.screen_time_record_occurrence (event_key);
CREATE INDEX screen_time_occurrence_event_at_idx ON base.screen_time_record_occurrence (event_at);
