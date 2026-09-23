with daily_metrics as (
    select
        'application' as grain,
        activity_date,
        device_key,
        platform,
        complete_seconds,
        inferred_seconds,
        total_seconds,
        complete_interval_parts,
        inferred_interval_parts
    from {{ ref('daily_screen_time') }}

    union all

    select
        'device' as grain,
        activity_date,
        device_key,
        platform,
        complete_seconds,
        inferred_seconds,
        total_seconds,
        complete_interval_parts,
        inferred_interval_parts
    from {{ ref('daily_screen_time_total') }}
)

select *
from daily_metrics
where complete_seconds < 0
   or inferred_seconds < 0
   or total_seconds < 0
   or not isfinite(complete_seconds)
   or not isfinite(inferred_seconds)
   or not isfinite(total_seconds)
   or abs(complete_seconds + inferred_seconds - total_seconds) > 0.000001
   or complete_interval_parts < 0
   or inferred_interval_parts < 0
