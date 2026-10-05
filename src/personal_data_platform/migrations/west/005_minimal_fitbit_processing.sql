-- Retire unused acquisition state before starting the minimal runtime.
SELECT CASE WHEN ((SELECT count(*) FROM ops.fitbit_scope) + (SELECT count(*) FROM ops.fitbit_notification) + (SELECT count(*) FROM ops.fitbit_notification_scope) + (SELECT count(*) FROM ops.fitbit_attempt) + (SELECT count(*) FROM ops.fitbit_scope_success) + (SELECT count(*) FROM ops.fitbit_bundle) + (SELECT count(*) FROM ops.fitbit_bundle_attempt) + (SELECT count(*) FROM ops.fitbit_bundle_chunk) + (SELECT count(*) FROM ops.fitbit_repair_cursor) + (SELECT count(*) FROM ops.fitbit_device_sync)) > 0 THEN error('Fitbit state must be empty before retirement') ELSE true END;
SELECT CASE WHEN EXISTS (SELECT 1 FROM ops.job_lock WHERE expires_at > now()) THEN error('Stop active writers before retirement') ELSE true END;

DROP TABLE ops.fitbit_device_sync;
DROP TABLE ops.fitbit_repair_cursor;
DROP TABLE ops.fitbit_bundle_chunk;
DROP TABLE ops.fitbit_bundle_attempt;
DROP TABLE ops.fitbit_bundle;
DROP TABLE ops.fitbit_scope_success;
DROP TABLE ops.fitbit_attempt;
DROP TABLE ops.fitbit_notification_scope;
DROP TABLE ops.fitbit_notification;
DROP TABLE ops.fitbit_scope;
