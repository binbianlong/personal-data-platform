{{ config(tags=['fitbit']) }}
select subject_key, record_id as sleep_id, start_at, end_at,
    cast(timezone('Asia/Tokyo', end_at) as date) as activity_date,
    source_date, sleep_minutes, offset_seconds, end_offset_seconds,
    sleep_type, is_main_sleep
from {{ source('fitbit_base', 'fitbit_sleep_session') }}
