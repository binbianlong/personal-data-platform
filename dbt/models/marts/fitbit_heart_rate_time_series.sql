{{ config(tags=['fitbit']) }}
select subject_key, record_id, start_at as sampled_at, value as beats_per_minute,
    offset_seconds, source_date, origin
from {{ source('fitbit_base', 'fitbit_heart_rate') }}
