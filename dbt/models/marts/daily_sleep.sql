{{ config(
    tags=['fitbit'],
    post_hook='drop view if exists {{ this.include(database=false, identifier=false) }}.daily_life'
) }}

with sleep_sessions as (
    select
        (end_at at time zone 'Asia/Tokyo')::date as sleep_date,
        sleep_session_id,
        start_at,
        end_at,
        sleep_minutes,
        awake_minutes,
        is_main_sleep
    from {{ source('base', 'sleep_session').include(database=false) }}
), main_sleep as (
    select sleep_date, sleep_session_id, start_at, end_at, sleep_minutes, awake_minutes
    from sleep_sessions
    where is_main_sleep is true
    qualify row_number() over (
        partition by sleep_date
        order by sleep_minutes desc nulls last, end_at desc, sleep_session_id
    ) = 1
), naps as (
    select
        sleep_date,
        case when count(sleep_minutes) = count(*) then sum(sleep_minutes) end as nap_minutes
    from sleep_sessions
    where is_main_sleep is false
    group by sleep_date
)

select
    sleep_date,
    main_sleep.sleep_session_id,
    main_sleep.start_at,
    main_sleep.end_at,
    main_sleep.sleep_minutes,
    main_sleep.awake_minutes,
    case when naps.sleep_date is null then 0 else naps.nap_minutes end as nap_minutes
from main_sleep
full outer join naps using (sleep_date)
