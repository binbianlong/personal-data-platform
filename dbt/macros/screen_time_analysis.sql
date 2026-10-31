{% macro screen_time_intervals(input_relation) %}
with events as (
    select *
    from {{ input_relation }}
),

next_events as (
    select
        *,
        first_value(case when state = 'start' then event_key end ignore nulls) over (
            partition by device_key, source_stream
            order by event_at, case when state = 'end' then 0 else 1 end, event_key
            rows between 1 following and unbounded following
        ) as next_start_event_key,
        first_value(case when state = 'end' then event_key end ignore nulls) over (
            partition by device_key, source_stream, bundle_id
            order by event_at, case when state = 'end' then 0 else 1 end, event_key
            rows between 1 following and unbounded following
        ) as same_app_end_event_key
    from events
),

start_candidates as (
    select
        start_event.*,
        same_app_end.event_at as same_app_end_at,
        same_app_end.duplicate_occurrence_count as end_duplicate_occurrence_count,
        next_start.event_at as next_start_at,
        next_start.duplicate_occurrence_count as next_start_duplicate_occurrence_count,
        same_app_end.event_key is not null
        and (
            next_start.event_key is null
            or same_app_end.event_at <= next_start.event_at
        ) as end_is_first
    from next_events as start_event
    left join events as same_app_end
        on start_event.same_app_end_event_key = same_app_end.event_key
    left join events as next_start
        on start_event.next_start_event_key = next_start.event_key
    where start_event.state = 'start'
),

paired_starts as (
    select
        *,
        case
            when end_is_first then same_app_end_at
            else next_start_at
        end as ended_at,
        case
            when end_is_first then same_app_end_event_key
            else null
        end as end_event_key,
        coalesce(
            case
                when end_is_first then end_duplicate_occurrence_count
                else next_start_duplicate_occurrence_count
            end,
            0
        ) as boundary_duplicate_occurrence_count,
        case
            when end_is_first then 'complete'
            when next_start_event_key is not null then 'inferred_end_from_next_start'
            else 'missing_end'
        end as interval_quality
    from start_candidates
),

start_intervals as (
    select
        md5(concat_ws(chr(31), event_key, coalesce(cast(epoch_us(ended_at) as varchar), '')))
            as interval_key,
        device_key,
        platform,
        bundle_id,
        event_at as started_at,
        ended_at,
        case
            when ended_at is not null and ended_at >= event_at
                then epoch(ended_at - event_at)
            else null
        end as duration_seconds,
        source_stream,
        interval_quality as quality,
        (
            duplicate_occurrence_count > 0
            or boundary_duplicate_occurrence_count > 0
        ) as has_duplicate_source,
        event_key as start_event_key,
        end_event_key
    from paired_starts
),

matched_ends as (
    select end_event_key
    from start_intervals
    where end_event_key is not null
),

unmatched_end_intervals as (
    select
        md5(concat_ws(chr(31), 'missing-start', end_event.event_key)) as interval_key,
        end_event.device_key,
        end_event.platform,
        end_event.bundle_id,
        null::timestamptz as started_at,
        end_event.event_at as ended_at,
        null::double as duration_seconds,
        end_event.source_stream,
        'missing_start' as quality,
        end_event.duplicate_occurrence_count > 0 as has_duplicate_source,
        null::varchar as start_event_key,
        end_event.event_key as end_event_key
    from events as end_event
    left join matched_ends
        on end_event.event_key = matched_ends.end_event_key
    where end_event.state = 'end'
      and matched_ends.end_event_key is null
)

select * from start_intervals
union all by name
select * from unmatched_end_intervals
{% endmacro %}

{% macro screen_time_daily(input_relation) %}
with valid_intervals as (
    select *
    from {{ input_relation }}
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
{% endmacro %}

{% macro sleep_preceding_screen_time(input_relation) %}
with clipped as (
    select sleep.subject_key, sleep.sleep_id, screen.device_key, screen.platform,
        greatest(screen.started_at, sleep.start_at - interval '2 hours') as started_at,
        least(screen.ended_at, sleep.start_at) as ended_at
    from {{ ref('fitbit_sleep_sessions') }} as sleep
    join {{ input_relation }} as screen
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
{% endmacro %}
