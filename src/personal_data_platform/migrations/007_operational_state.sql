-- Daily job outcomes are recorded once; collector success remains stream-owned.
DROP TABLE ops.reconciliation_run;
DELETE FROM ops.heartbeat WHERE monitor_name = 'daily';
