-- Minute ends are derived; the provider does not supply original sample counts.
CREATE TABLE base.fitbit_heart_rate_minute_next (
    subject_key VARCHAR NOT NULL,
    data_source_family VARCHAR NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    aggregation_version VARCHAR NOT NULL,
    average DOUBLE NOT NULL,
    minimum DOUBLE NOT NULL,
    maximum DOUBLE NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_key VARCHAR NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subject_key, data_source_family, start_at, aggregation_version),
    CHECK (date_trunc('minute', start_at) = start_at),
    CHECK (isfinite(average) AND isfinite(minimum) AND isfinite(maximum)),
    CHECK (minimum >= 0 AND minimum <= average AND average <= maximum)
);

INSERT INTO base.fitbit_heart_rate_minute_next
SELECT subject_key, data_source_family, start_at, aggregation_version,
       average, minimum, maximum, fetched_at, source_key, loaded_at
FROM base.fitbit_heart_rate_minute;

DROP TABLE base.fitbit_heart_rate_minute;
ALTER TABLE base.fitbit_heart_rate_minute_next RENAME TO fitbit_heart_rate_minute;

CREATE OR REPLACE VIEW marts.fitbit_heart_rate_minute_time_series AS
SELECT subject_key, data_source_family, start_at, start_at + INTERVAL '1 minute' AS end_at,
       average AS mean_minute_heart_rate, minimum AS min_heart_rate,
       maximum AS max_heart_rate, aggregation_version, fetched_at, source_key
FROM base.fitbit_heart_rate_minute;
