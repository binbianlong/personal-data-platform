{{ config(tags=['fitbit']) }}
with intervals as (
    select subject_key, start_at, end_at, value, 'steps' as metric
    from {{ source('fitbit_base', 'fitbit_steps') }}
    union all
    select subject_key, start_at, end_at, value, 'active_zone_minutes' as metric
    from {{ source('fitbit_base', 'fitbit_active_zone') }}
), parts as (
    select subject_key, metric, cast(day_start as date) as activity_date,
        value * epoch(least(end_at, timezone('Asia/Tokyo', day_start + interval '1 day'))
            - greatest(start_at, timezone('Asia/Tokyo', day_start)))
            / epoch(end_at - start_at) as value
    from intervals,
        lateral generate_series(
            date_trunc('day', timezone('Asia/Tokyo', start_at)),
            date_trunc('day', timezone('Asia/Tokyo', end_at - interval '1 microsecond')),
            interval '1 day'
        ) as days(day_start)
), daily as (
    select subject_key, activity_date,
        sum(value) filter (where metric = 'steps') as steps,
        sum(value) filter (where metric = 'active_zone_minutes') as active_zone_minutes
    from parts group by subject_key, activity_date
), heart as (
    select * from {{ ref('daily_fitbit_heart_rate_minute') }}
), resting as (
    select subject_key, source_date as activity_date, value as resting_heart_rate
    from {{ source('fitbit_base', 'fitbit_resting_heart_rate') }}
), sleep as (
    select subject_key, activity_date, sum(sleep_minutes) as sleep_minutes,
        count(*) as sleep_sessions
    from {{ ref('fitbit_sleep_sessions') }} group by subject_key, activity_date
), dates as (
    select subject_key, activity_date from daily
    union select subject_key, activity_date from heart
    union select subject_key, activity_date from resting
    union select subject_key, activity_date from sleep
)
select dates.*, daily.steps, daily.active_zone_minutes,
    heart.mean_minute_heart_rate, heart.min_heart_rate, heart.max_heart_rate, heart.observed_heart_rate_minutes,
    resting.resting_heart_rate, sleep.sleep_minutes, sleep.sleep_sessions
from dates
left join daily using (subject_key, activity_date)
left join heart using (subject_key, activity_date)
left join resting using (subject_key, activity_date)
left join sleep using (subject_key, activity_date)
