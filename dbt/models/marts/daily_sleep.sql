{{ config(tags=['fitbit']) }}

select
    (end_at at time zone 'Asia/Tokyo')::date as sleep_date,
    sleep_session_id,
    start_at,
    end_at,
    sleep_minutes,
    awake_minutes
from {{ source('base', 'sleep_session').include(database=false) }}
where is_main_sleep is true
qualify row_number() over (
    partition by sleep_date
    order by sleep_minutes desc nulls last, end_at desc, sleep_session_id
) = 1
