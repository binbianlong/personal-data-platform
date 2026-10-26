DROP INDEX ops.screen_time_record_scope;
DROP INDEX ops.screen_time_record_event;
ALTER TABLE ops.screen_time_record DROP COLUMN segment_filename;
CREATE INDEX screen_time_record_scope ON ops.screen_time_record
    (device_key, source_stream, segment_key);
CREATE INDEX screen_time_record_event ON ops.screen_time_record (event_key);
