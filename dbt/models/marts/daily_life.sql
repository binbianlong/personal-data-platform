{{ config(post_hook='drop view if exists {{ this.include(database=false, identifier=false) }}.daily_sleep') }}

with sleep as (
    select
        (end_at at time zone 'Asia/Tokyo')::date as activity_date,
        start_at,
        end_at,
        sleep_minutes,
        awake_minutes
    from {{ source('base', 'sleep_session').include(database=false) }}
    where is_main_sleep is true
    qualify row_number() over (
        partition by activity_date
        order by sleep_minutes desc nulls last, end_at desc, sleep_session_id
    ) = 1
)

select
    activity_date,
    sleep.start_at as sleep_start_at,
    sleep.end_at as sleep_end_at,
    sleep.sleep_minutes,
    sleep.awake_minutes,
    step_totals.steps,
    zone_totals.active_zone_minutes,
    iphone.total_seconds as iphone_total_seconds,
    iphone.unresolved_event_count as iphone_unresolved_event_count,
    mac.total_seconds as mac_total_seconds,
    mac.unresolved_event_count as mac_unresolved_event_count
from sleep
full outer join {{ ref('daily_steps').include(database=false) }} as step_totals using (activity_date)
full outer join {{ ref('daily_active_zone').include(database=false) }} as zone_totals using (activity_date)
full outer join {{ ref('daily_iphone_screen_time_total').include(database=false) }} as iphone using (activity_date)
full outer join {{ ref('daily_mac_screen_time_total').include(database=false) }} as mac using (activity_date)
