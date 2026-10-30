{{ config(tags=['fitbit']) }}
select subject_key, record_id as exercise_id, start_at, end_at,
    cast(timezone('Asia/Tokyo', start_at) as date) as activity_date,
    source_date, exercise_type, display_name, active_duration_seconds,
    epoch(end_at - start_at) as elapsed_seconds, offset_seconds, end_offset_seconds
from {{ source('fitbit_base', 'fitbit_exercise') }}
