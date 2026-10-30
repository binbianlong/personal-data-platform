{{ config(tags=['fitbit']) }}
with intervals as (
    select subject_key, start_at, end_at, value, metric, null::varchar as zone
    from {{ source('fitbit_base', 'fitbit_activity_interval') }}
    union all
    select subject_key, start_at, end_at, distance_meters, 'distance', null
    from {{ source('fitbit_base', 'fitbit_distance_interval') }}
    union all
    select subject_key, start_at, end_at, epoch(end_at - start_at), 'sedentary', null
    from {{ source('fitbit_base', 'fitbit_sedentary_period') }}
    union all
    select subject_key, start_at, end_at, epoch(end_at - start_at), 'heart-rate-zone', zone
    from {{ source('fitbit_base', 'fitbit_heart_rate_zone_interval') }}
), parts as (
    select subject_key, metric, zone, cast(day_start as date) as activity_date,
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
        sum(value) filter (where metric = 'active-zone-minutes') as active_zone_minutes,
        sum(value) filter (where metric = 'distance') as distance_meters,
        sum(value) filter (where metric = 'sedentary') as sedentary_seconds,
        sum(value) filter (where metric = 'heart-rate-zone' and zone = 'LIGHT') as heart_rate_zone_light_seconds,
        sum(value) filter (where metric = 'heart-rate-zone' and zone = 'MODERATE') as heart_rate_zone_moderate_seconds,
        sum(value) filter (where metric = 'heart-rate-zone' and zone = 'VIGOROUS') as heart_rate_zone_vigorous_seconds,
        sum(value) filter (where metric = 'heart-rate-zone' and zone = 'PEAK') as heart_rate_zone_peak_seconds
    from parts group by subject_key, activity_date
), heart as (
    select * from {{ ref('daily_fitbit_heart_rate_minute') }}
), resting as (
    select subject_key, source_date as activity_date, beats_per_minute as resting_heart_rate
    from {{ source('fitbit_base', 'fitbit_resting_heart_rate_daily') }}
), sleep as (
    select subject_key, activity_date, sum(sleep_minutes) as sleep_minutes,
        count(*) as sleep_sessions
    from {{ ref('fitbit_sleep_sessions') }} group by subject_key, activity_date
), hrv as (
    select subject_key, source_date as activity_date, rmssd_ms, deep_sleep_rmssd_ms
    from {{ source('fitbit_base', 'fitbit_hrv_daily') }}
), respiratory as (
    select subject_key, source_date as activity_date, breaths_per_minute
    from {{ source('fitbit_base', 'fitbit_respiratory_rate_daily') }}
), oxygen as (
    select subject_key, source_date as activity_date, average_percent, lower_bound_percent,
        upper_bound_percent, standard_deviation_percent
    from {{ source('fitbit_base', 'fitbit_oxygen_saturation_daily') }}
), temperature as (
    select subject_key, source_date as activity_date,
        nightly_temperature_celsius - baseline_temperature_celsius as delta_celsius
    from {{ source('fitbit_base', 'fitbit_sleep_temperature_daily') }}
), vo2 as (
    select subject_key, source_date as activity_date, vo2_max_ml_kg_min, estimated
    from {{ source('fitbit_base', 'fitbit_vo2_max_daily') }}
), dates as (
    select subject_key, activity_date from daily
    union select subject_key, activity_date from heart
    union select subject_key, activity_date from resting
    union select subject_key, activity_date from sleep
    union select subject_key, activity_date from hrv
    union select subject_key, activity_date from respiratory
    union select subject_key, activity_date from oxygen
    union select subject_key, activity_date from temperature
    union select subject_key, activity_date from vo2
)
select dates.*, daily.steps, daily.active_zone_minutes,
    heart.mean_minute_heart_rate, heart.min_heart_rate, heart.max_heart_rate, heart.observed_heart_rate_minutes,
    resting.resting_heart_rate, sleep.sleep_minutes, sleep.sleep_sessions,
    hrv.rmssd_ms as hrv_rmssd_ms, hrv.deep_sleep_rmssd_ms as deep_sleep_hrv_rmssd_ms,
    respiratory.breaths_per_minute as respiratory_rate_breaths_per_minute,
    oxygen.average_percent as spo2_average_percent,
    oxygen.lower_bound_percent as spo2_lower_bound_percent,
    oxygen.upper_bound_percent as spo2_upper_bound_percent,
    oxygen.standard_deviation_percent as spo2_standard_deviation_percent,
    temperature.delta_celsius as sleep_temperature_delta_celsius,
    vo2.vo2_max_ml_kg_min, vo2.estimated as vo2_max_estimated,
    daily.distance_meters, daily.sedentary_seconds,
    daily.heart_rate_zone_light_seconds, daily.heart_rate_zone_moderate_seconds,
    daily.heart_rate_zone_vigorous_seconds, daily.heart_rate_zone_peak_seconds
from dates
left join daily using (subject_key, activity_date)
left join heart using (subject_key, activity_date)
left join resting using (subject_key, activity_date)
left join sleep using (subject_key, activity_date)
left join hrv using (subject_key, activity_date)
left join respiratory using (subject_key, activity_date)
left join oxygen using (subject_key, activity_date)
left join temperature using (subject_key, activity_date)
left join vo2 using (subject_key, activity_date)
