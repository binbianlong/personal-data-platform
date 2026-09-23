select
    'application' as grain,
    activity_date,
    device_key,
    platform,
    bundle_id,
    count(*) as row_count
from {{ ref('daily_screen_time') }}
group by activity_date, device_key, platform, bundle_id
having count(*) > 1

union all

select
    'device' as grain,
    activity_date,
    device_key,
    platform,
    null::varchar as bundle_id,
    count(*) as row_count
from {{ ref('daily_screen_time_total') }}
group by activity_date, device_key, platform
having count(*) > 1
