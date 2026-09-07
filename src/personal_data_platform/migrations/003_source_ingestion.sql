ALTER TABLE ops.ingestion_metadata
    ADD COLUMN source_id VARCHAR DEFAULT 'screen_time';
ALTER TABLE ops.ingestion_metadata
    ALTER COLUMN source_id SET NOT NULL;

ALTER TABLE ops.ingestion_metadata
    ADD COLUMN schema_version UINTEGER DEFAULT 1;
ALTER TABLE ops.ingestion_metadata
    ALTER COLUMN schema_version SET NOT NULL;

ALTER TABLE ops.ingestion_metadata ADD COLUMN subject_key VARCHAR;
ALTER TABLE ops.ingestion_metadata ADD COLUMN logical_key VARCHAR;

UPDATE ops.ingestion_metadata
SET subject_key = device_key, logical_key = segment_key;

ALTER TABLE ops.ingestion_metadata ALTER COLUMN device_key DROP NOT NULL;
ALTER TABLE ops.ingestion_metadata ALTER COLUMN segment_key DROP NOT NULL;
