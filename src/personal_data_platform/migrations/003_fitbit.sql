-- Fitbit metrics and acquisition coverage; all times are UTC.

CREATE TABLE IF NOT EXISTS base.fitbit_steps (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_heart_rate (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_resting_heart_rate (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_active_zone (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_sleep (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_sleep_stage (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);

CREATE TABLE IF NOT EXISTS base.fitbit_sleep_wake (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    cursor_at TIMESTAMPTZ NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    value DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE,
    parent_id VARCHAR,
    category VARCHAR,
    is_main_sleep BOOLEAN,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at IS NULL OR end_at > start_at),
    CHECK (value IS NULL OR (isfinite(value) AND value >= 0))
);


CREATE TABLE IF NOT EXISTS ops.fitbit_coverage (
    subject_key VARCHAR NOT NULL,
    data_type VARCHAR NOT NULL,
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    origin VARCHAR NOT NULL,
    source_key VARCHAR NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    PRIMARY KEY (subject_key, data_type, range_start),
    CHECK (range_end > range_start)
);

-- Keep ordering after an ID moves between ranges and is later deleted.
CREATE TABLE IF NOT EXISTS ops.fitbit_deleted_record (
    subject_key VARCHAR NOT NULL,
    data_type VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    origin VARCHAR NOT NULL,
    source_key VARCHAR NOT NULL,
    PRIMARY KEY (subject_key, data_type, record_id)
);
