{{ config(tags=['fitbit']) }}
with clipped as (
    select sleep.subject_key, sleep.sleep_id, screen.device_key, screen.platform,
        greatest(screen.started_at, sleep.start_at - interval '2 hours') as started_at,
        least(screen.ended_at, sleep.start_at) as ended_at
    from {{ ref('fitbit_sleep_sessions') }} as sleep
    join {{ ref('screen_time_interval') }} as screen
        on screen.started_at < sleep.start_at
        and screen.ended_at > sleep.start_at - interval '2 hours'
        and screen.quality in ('complete', 'inferred_end_from_next_start')
), previous as (
    select *, max(ended_at) over (
        partition by subject_key, sleep_id, device_key, platform
        order by started_at, ended_at rows between unbounded preceding and 1 preceding
    ) as previous_end from clipped
), groups as (
    select *, sum(case when previous_end is null or started_at > previous_end then 1 else 0 end)
        over (partition by subject_key, sleep_id, device_key, platform
              order by started_at, ended_at rows unbounded preceding) as interval_group
    from previous
), merged as (
    select subject_key, sleep_id, device_key, platform, interval_group,
        min(started_at) as started_at, max(ended_at) as ended_at
    from groups group by subject_key, sleep_id, device_key, platform, interval_group
)
select subject_key, sleep_id, device_key, platform,
    sum(epoch(ended_at - started_at)) as screen_time_seconds
from merged group by subject_key, sleep_id, device_key, platform
