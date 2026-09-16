-- Ingestion state is committed alongside events and the Raw success receipt.
DROP TABLE ops.screen_time_checkpoint;

CREATE TABLE ops.screen_time_segment (
    device_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    segment_key VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    object_key VARCHAR NOT NULL,
    source_segment_name VARCHAR,
    name_ambiguous BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (device_key, source_stream, segment_key)
);

-- Decoded fields permit choosing another physical copy after a correction.
-- Original protobuf/SEGB bytes remain exclusively in Raw and retained legacy rows.
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

-- Bootstrap only once, in this migration transaction. Keep all legacy tables intact.
INSERT INTO ops.screen_time_segment
WITH observations AS (
    SELECT device_key, source_stream, segment_key, observed_at, object_key,
           CASE WHEN segment_kind = 'events' THEN source_segment_name END AS name
    FROM base.screen_time_segment_observation
    UNION ALL
    SELECT device_key, source_stream, segment_key, observed_at, object_key, NULL
    FROM base.screen_time_record_occurrence
), ranked AS (
    SELECT *, min(name) OVER scope AS first_name,
           count(DISTINCT name) OVER scope > 1 AS ambiguous
    FROM observations
    WINDOW scope AS (PARTITION BY device_key, source_stream, segment_key)
)
SELECT device_key, source_stream, segment_key, observed_at, object_key, first_name, ambiguous
FROM ranked
QUALIFY row_number() OVER (
    PARTITION BY device_key, source_stream, segment_key ORDER BY observed_at DESC, object_key DESC
) = 1;

CREATE TEMP TABLE screen_time_seed AS
SELECT * EXCLUDE (original_payload), ops.screen_time_physical_id(
    device_key, source_stream, segment_key, record_offset, record_metadata_offset,
    record_timestamp_cocoa, sha256(original_payload)
) AS physical_id
FROM base.screen_time_record_occurrence
QUALIFY row_number() OVER (
    PARTITION BY object_key, record_offset ORDER BY record_metadata_offset DESC
) = 1;

INSERT INTO ops.screen_time_record
SELECT r.physical_id, r.device_key, r.source_stream, r.segment_key,
       r.record_offset, r.record_metadata_offset, r.payload_length, r.record_timestamp_cocoa,
       r.event_key, r.bundle_id, r.event_at, CASE WHEN r.in_foreground THEN 'start' ELSE 'end' END,
       r.transition_reason, r.kind, r.app_version, r.app_build, r.platform_flag,
       r.parser_version, r.unknown_field_count, r.segment_filename, r.observed_at, r.object_key,
       r.object_key = s.object_key, true
FROM screen_time_seed r
JOIN ops.screen_time_segment s USING (device_key, source_stream, segment_key)
WHERE upper(r.record_state) = 'WRITTEN' AND r.crc_passed IS DISTINCT FROM false
  AND r.record_kind = 'event' AND r.event_key IS NOT NULL
QUALIFY row_number() OVER (
    PARTITION BY physical_id ORDER BY r.observed_at DESC, r.object_key DESC
) = 1;

INSERT INTO ops.screen_time_tombstone
SELECT physical_id, device_key, source_stream, segment_key, target_segment_name,
       target_offset, target_length, target_event_timestamp, deletion_reason,
       observed_at, object_key, true, 'unmatched'
FROM screen_time_seed
WHERE upper(record_state) = 'WRITTEN' AND crc_passed IS DISTINCT FROM false
  AND record_kind = 'tombstone'
QUALIFY row_number() OVER (
    PARTITION BY physical_id ORDER BY observed_at DESC, object_key DESC
) = 1;
DROP TABLE screen_time_seed;

-- Name uniqueness and all physical coordinates are required for deletion matching.
CREATE MACRO ops.screen_time_matching_record(keys) AS TABLE (
WITH tombstones AS (
    SELECT * FROM ops.screen_time_tombstone WHERE physical_id IN (SELECT unnest(keys))
), targets AS (
    SELECT DISTINCT device_key, source_stream, target_segment_name FROM tombstones
), names AS (
    SELECT s.device_key, s.source_stream, s.source_segment_name, min(s.segment_key) AS segment_key
    FROM ops.screen_time_segment s
    JOIN targets t ON s.device_key = t.device_key AND s.source_stream = t.source_stream
        AND s.source_segment_name = t.target_segment_name
    GROUP BY s.device_key, s.source_stream, s.source_segment_name
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

INSERT INTO ops.screen_time_deletion_match
SELECT * FROM ops.screen_time_matching_record(
    (SELECT list(physical_id) FROM ops.screen_time_tombstone)
);
UPDATE ops.screen_time_tombstone t SET resolution = CASE
    WHEN deletion_reason NOT IN (1, 2) OR deletion_reason IS NULL THEN 'unsupported_reason'
    WHEN NOT EXISTS (SELECT 1 FROM ops.screen_time_deletion_match m
                     WHERE m.tombstone_id = t.physical_id) THEN 'unmatched'
    WHEN deletion_reason = 1 THEN 'ttl_history_retained'
    ELSE 'user_deletion_applied' END;

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
-- Merge only reproducible existing events. The event writer can contain newer
-- deletions/corrections than the archived observations; observation time alone
-- cannot order those changes. Never replace such results with stale history.
CREATE TEMP TABLE screen_time_merge_expected AS
SELECT * FROM ops.screen_time_resolve(
    (SELECT list(DISTINCT event_key) FROM ops.screen_time_record)
);
SELECT CASE WHEN EXISTS (
    SELECT * EXCLUDE (object_key, segment_key, segment_filename, record_offset,
                      record_metadata_offset, observed_at, loaded_at)
    FROM base.screen_time_event
    EXCEPT
    SELECT * EXCLUDE (object_key, segment_key, segment_filename, record_offset,
                      record_metadata_offset, observed_at)
    FROM screen_time_merge_expected
) THEN error('Screen Time migration blocked: existing events cannot be reconstructed from legacy history; restore checkpoint-backed history before retrying') END;

-- Existing rows win when analytical fields and active state agree, preserving
-- their provenance and loaded_at. Only missing event keys are bootstrapped.
INSERT INTO base.screen_time_event
SELECT *, current_timestamp FROM screen_time_merge_expected
ON CONFLICT (event_key) DO NOTHING;
DROP TABLE screen_time_merge_expected;
