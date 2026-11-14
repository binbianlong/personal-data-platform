with sleep as (
    select
        sleep_date as activity_date,
        start_at,
        end_at,
        sleep_minutes,
        awake_minutes
    from {{ ref('daily_sleep').include(database=false) }}
)

select
    activity_date,
    sleep.start_at as sleep_start_at,
    sleep.end_at as sleep_end_at,
    sleep.sleep_minutes,
    sleep.awake_minutes,
    step_totals.steps,
    zone_totals.active_zone_minutes,
    iphone.observed_seconds as iphone_observed_seconds,
    iphone.inferred_seconds as iphone_inferred_seconds,
    iphone.total_seconds as iphone_total_seconds,
    iphone.unresolved_event_count as iphone_unresolved_event_count,
    mac.observed_seconds as mac_observed_seconds,
    mac.inferred_seconds as mac_inferred_seconds,
    mac.total_seconds as mac_total_seconds,
    mac.unresolved_event_count as mac_unresolved_event_count
from sleep
full outer join {{ ref('daily_steps').include(database=false) }} as step_totals using (activity_date)
full outer join {{ ref('daily_active_zone').include(database=false) }} as zone_totals using (activity_date)
full outer join {{ ref('daily_iphone_screen_time_total').include(database=false) }} as iphone using (activity_date)
full outer join {{ ref('daily_mac_screen_time_total').include(database=false) }} as mac using (activity_date)
