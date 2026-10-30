-- Native Google Health metrics. Provider dates remain independent of physical time.

CREATE TABLE base.fitbit_hrv_daily (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    source_date DATE NOT NULL,
    rmssd_ms DOUBLE,
    deep_sleep_rmssd_ms DOUBLE,
    non_rem_heart_rate_bpm BIGINT,
    entropy DOUBLE,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    UNIQUE (subject_key, source_date)
);

CREATE TABLE base.fitbit_hrv_sample (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    sample_at TIMESTAMPTZ NOT NULL,
    offset_seconds INTEGER,
    source_date DATE NOT NULL,
    rmssd_ms DOUBLE,
    sdnn_ms DOUBLE,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id)
);

CREATE TABLE base.fitbit_respiratory_rate_daily (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    source_date DATE NOT NULL,
    breaths_per_minute DOUBLE NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    UNIQUE (subject_key, source_date)
);

CREATE TABLE base.fitbit_respiratory_rate_sleep_summary (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    sample_at TIMESTAMPTZ NOT NULL,
    offset_seconds INTEGER,
    source_date DATE NOT NULL,
    full_breaths_per_minute DOUBLE,
    full_standard_deviation DOUBLE,
    full_signal_to_noise DOUBLE,
    deep_breaths_per_minute DOUBLE,
    deep_standard_deviation DOUBLE,
    deep_signal_to_noise DOUBLE,
    light_breaths_per_minute DOUBLE,
    light_standard_deviation DOUBLE,
    light_signal_to_noise DOUBLE,
    rem_breaths_per_minute DOUBLE,
    rem_standard_deviation DOUBLE,
    rem_signal_to_noise DOUBLE,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id)
);

CREATE TABLE base.fitbit_oxygen_saturation_daily (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    source_date DATE NOT NULL,
    average_percent DOUBLE NOT NULL,
    lower_bound_percent DOUBLE NOT NULL,
    upper_bound_percent DOUBLE NOT NULL,
    standard_deviation_percent DOUBLE,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    UNIQUE (subject_key, source_date)
);

CREATE TABLE base.fitbit_sleep_temperature_daily (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    source_date DATE NOT NULL,
    nightly_temperature_celsius DOUBLE NOT NULL,
    baseline_temperature_celsius DOUBLE,
    relative_nightly_stddev_30d_celsius DOUBLE,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    UNIQUE (subject_key, source_date)
);

CREATE TABLE base.fitbit_exercise (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE NOT NULL,
    exercise_type VARCHAR NOT NULL,
    display_name VARCHAR NOT NULL,
    active_duration_seconds DOUBLE,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at > start_at)
);

CREATE TABLE base.fitbit_vo2_max_daily (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    source_date DATE NOT NULL,
    vo2_max_ml_kg_min DOUBLE NOT NULL,
    estimated BOOLEAN,
    cardio_fitness_level VARCHAR,
    covariance DOUBLE,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    UNIQUE (subject_key, source_date)
);

CREATE TABLE base.fitbit_sedentary_period (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at > start_at)
);

CREATE TABLE base.fitbit_distance_interval (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE NOT NULL,
    distance_meters DOUBLE NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at > start_at)
);

CREATE TABLE base.fitbit_heart_rate_zone_interval (
    subject_key VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER,
    source_date DATE NOT NULL,
    zone VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, record_id),
    CHECK (end_at > start_at)
);

ALTER TABLE base.fitbit_sleep_session ADD COLUMN minutes_awake BIGINT;

ALTER TABLE base.fitbit_sleep_session ADD COLUMN minutes_in_sleep_period BIGINT;

ALTER TABLE base.fitbit_sleep_session ADD COLUMN minutes_to_fall_asleep BIGINT;

ALTER TABLE base.fitbit_sleep_session ADD COLUMN minutes_after_wake_up BIGINT;
