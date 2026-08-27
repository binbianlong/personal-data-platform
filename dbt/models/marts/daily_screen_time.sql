with valid_intervals as (
    select *
    from {{ ref('screen_time_interval') }}
    where quality in ('complete', 'inferred_end_from_next_start')
      and started_at is not null
      and ended_at is not null
      and ended_at >= started_at
),

local_days as (
    select
        interval.*,
        generated.local_day::date as activity_date,
        generated.local_day::timestamp at time zone 'Asia/Tokyo' as day_started_at,
        (generated.local_day::timestamp + interval '1 day') at time zone 'Asia/Tokyo'
            as day_ended_at
    from valid_intervals as interval
    cross join lateral generate_series(
        date_trunc('day', interval.started_at at time zone 'Asia/Tokyo'),
        date_trunc(
            'day',
            (interval.ended_at - interval '1 microsecond') at time zone 'Asia/Tokyo'
        ),
        interval '1 day'
    ) as generated(local_day)
    where interval.ended_at > interval.started_at
),

split_intervals as (
    select
        activity_date,
        device_key,
        platform,
        bundle_id,
        quality,
        epoch(
            least(ended_at, day_ended_at) - greatest(started_at, day_started_at)
        ) as duration_seconds
    from local_days
)

select
    activity_date,
    device_key,
    platform,
    bundle_id,
    coalesce(
        sum(duration_seconds) filter (where quality = 'complete'),
        0
    ) as complete_seconds,
    coalesce(
        sum(duration_seconds) filter (where quality = 'inferred_end_from_next_start'),
        0
    ) as inferred_seconds,
    sum(duration_seconds) as total_seconds,
    count(*) filter (where quality = 'complete') as complete_interval_parts,
    count(*) filter (
        where quality = 'inferred_end_from_next_start'
    ) as inferred_interval_parts
from split_intervals
group by all
