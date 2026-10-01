{{ config(tags=['fitbit']) }}
select subject_key, record_id, start_at, end_at, value as steps,
    offset_seconds, end_offset_seconds, source_date, origin
from {{ source('fitbit_base', 'fitbit_steps') }}
