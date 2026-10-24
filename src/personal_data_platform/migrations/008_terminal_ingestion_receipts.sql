-- Keep active, confirmed outcomes; analytical history is retained independently.
CREATE TABLE ops.ingestion_metadata_next (
    object_key VARCHAR PRIMARY KEY,
    source_id VARCHAR NOT NULL,
    schema_version UINTEGER NOT NULL,
    subject_key VARCHAR NOT NULL,
    source_stream VARCHAR NOT NULL,
    logical_key VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    byte_size UBIGINT NOT NULL,
    status VARCHAR NOT NULL CHECK (status IN ('succeeded', 'failed')),
    parser_version VARCHAR,
    record_count UINTEGER,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    error_type VARCHAR,
    error_message VARCHAR,
    retry_count UINTEGER NOT NULL DEFAULT 0,
    storage_created_at TIMESTAMPTZ,
    storage_generation UBIGINT
);

INSERT INTO ops.ingestion_metadata_next
SELECT object_key, source_id, schema_version, subject_key, source_stream, logical_key,
       observed_at, content_sha256, byte_size,
       CASE WHEN status = 'loading' THEN 'failed' ELSE status END,
       parser_version, record_count, started_at,
       CASE WHEN status = 'loading' THEN current_timestamp ELSE completed_at END,
       CASE WHEN status = 'loading' THEN 'InterruptedIngestion' ELSE error_type END,
       CASE WHEN status = 'loading' THEN 'Retry interrupted ingestion from retained Raw'
            ELSE error_message END,
       retry_count, storage_created_at, storage_generation
FROM ops.ingestion_metadata
WHERE retention_expired_at IS NULL;

DROP TABLE ops.ingestion_metadata;
ALTER TABLE ops.ingestion_metadata_next RENAME TO ingestion_metadata;
