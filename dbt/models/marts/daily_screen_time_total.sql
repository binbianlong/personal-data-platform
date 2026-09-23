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
