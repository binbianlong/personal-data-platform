-- Daily collector state lives with the shared ingestion ledger, rather than GCS.
CREATE TABLE IF NOT EXISTS ops.fitbit_daily_state (
    subject_key VARCHAR PRIMARY KEY,
    completed_through DATE,
    sync_covered_through DATE,
    last_sync_time VARCHAR,
    last_daily_run DATE,
    recheck_json VARCHAR
);

CREATE TABLE IF NOT EXISTS ops.fitbit_batch_intent (
    raw_key VARCHAR PRIMARY KEY,
    subject_key VARCHAR NOT NULL,
    details_json VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
