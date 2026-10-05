-- Complete UTC minute windows from the Google wearables heart rate rollup.
CREATE TABLE IF NOT EXISTS base.fitbit_heart_rate_minute (
    subject_key VARCHAR NOT NULL,
    data_source_family VARCHAR NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    aggregation_version VARCHAR NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    average DOUBLE NOT NULL,
    minimum DOUBLE NOT NULL,
    maximum DOUBLE NOT NULL,
    sample_count BIGINT,
    origin VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, data_source_family, start_at, aggregation_version),
    CHECK (end_at = start_at + INTERVAL '1 minute'),
    CHECK (date_trunc('minute', start_at) = start_at),
    CHECK (isfinite(average) AND isfinite(minimum) AND isfinite(maximum)),
    CHECK (minimum >= 0 AND minimum <= average AND average <= maximum),
    CHECK (sample_count IS NULL OR sample_count > 0)
);

CREATE TABLE IF NOT EXISTS ops.fitbit_minute_coverage (
    subject_key VARCHAR NOT NULL,
    data_source_family VARCHAR NOT NULL,
    aggregation_version VARCHAR NOT NULL,
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    origin VARCHAR NOT NULL,
    source_key VARCHAR NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    PRIMARY KEY (subject_key, data_source_family, aggregation_version, range_start),
    CHECK (range_end > range_start)
);
