-- Forward-only Fitbit acquisition state. Old coverage has no lossless digest,
-- so it is never eligible for Raw omission until refreshed by a new Raw.
ALTER TABLE ops.fitbit_coverage ADD COLUMN source_sha256 VARCHAR DEFAULT '';

CREATE TABLE IF NOT EXISTS ops.fitbit_raw_intent (
    receipt_key VARCHAR NOT NULL,
    work_index INTEGER NOT NULL,
    subject_key VARCHAR NOT NULL,
    data_type VARCHAR NOT NULL,
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    raw_key VARCHAR PRIMARY KEY,
    fetched_at TIMESTAMPTZ NOT NULL,
    CHECK (range_end > range_start)
);
