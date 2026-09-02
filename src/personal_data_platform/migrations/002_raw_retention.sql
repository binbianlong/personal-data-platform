ALTER TABLE ops.ingestion_metadata
    ADD COLUMN storage_created_at TIMESTAMPTZ;

ALTER TABLE ops.ingestion_metadata
    ADD COLUMN storage_generation UBIGINT;

ALTER TABLE ops.ingestion_metadata
    ADD COLUMN retention_expired_at TIMESTAMPTZ;
