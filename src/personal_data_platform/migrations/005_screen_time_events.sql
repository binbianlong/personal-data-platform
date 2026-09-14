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

-- A fence for the external checkpoint, not a copy of its contents.
CREATE TABLE ops.screen_time_checkpoint (
    singleton BOOLEAN PRIMARY KEY CHECK (singleton),
    state_id VARCHAR NOT NULL,
    revision UBIGINT NOT NULL
);
