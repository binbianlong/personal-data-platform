with application_totals as (
    select
        activity_date,
        device_key,
        platform,
        sum(complete_seconds) as complete_seconds,
        sum(inferred_seconds) as inferred_seconds,
        sum(total_seconds) as total_seconds,
        sum(complete_interval_parts) as complete_interval_parts,
        sum(inferred_interval_parts) as inferred_interval_parts
    from {{ ref('daily_screen_time') }}
    group by activity_date, device_key, platform
)

select
    coalesce(app.activity_date, total.activity_date) as activity_date,
    coalesce(app.device_key, total.device_key) as device_key,
    coalesce(app.platform, total.platform) as platform,
    app.total_seconds as application_seconds,
    total.total_seconds as device_seconds
from application_totals as app
full outer join {{ ref('daily_screen_time_total') }} as total
    using (activity_date, device_key, platform)
where app.activity_date is null
   or total.activity_date is null
   or abs(app.complete_seconds - total.complete_seconds) > 0.000001
   or abs(app.inferred_seconds - total.inferred_seconds) > 0.000001
   or abs(app.total_seconds - total.total_seconds) > 0.000001
   or app.complete_interval_parts <> total.complete_interval_parts
   or app.inferred_interval_parts <> total.inferred_interval_parts
