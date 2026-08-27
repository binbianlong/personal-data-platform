with events as (
    select *
    from {{ ref('screen_time_transition') }}
),

start_candidates as (
    select
        start_event.event_key as start_event_key,
        min(candidate.event_at) filter (
            where candidate.state = 'end'
              and candidate.bundle_id = start_event.bundle_id
        ) as same_app_end_at,
        min(candidate.event_at) filter (
            where candidate.state = 'start'
        ) as next_start_at
    from events as start_event
    left join events as candidate
        on candidate.device_key = start_event.device_key
        and candidate.source_stream = start_event.source_stream
        and (
            candidate.event_at > start_event.event_at
            or (
                candidate.event_at = start_event.event_at
                and candidate.event_key > start_event.event_key
            )
        )
        and (
            candidate.state = 'start'
            or (candidate.state = 'end' and candidate.bundle_id = start_event.bundle_id)
        )
    where start_event.state = 'start'
    group by start_event.event_key
),

paired_starts as (
    select
        start_event.*,
        case
            when candidate.same_app_end_at is not null
             and (
                 candidate.next_start_at is null
                 or candidate.same_app_end_at <= candidate.next_start_at
             )
                then candidate.same_app_end_at
            when candidate.next_start_at is not null
                then candidate.next_start_at
            else null
        end as ended_at,
        case
            when candidate.same_app_end_at is not null
             and (
                 candidate.next_start_at is null
                 or candidate.same_app_end_at <= candidate.next_start_at
             )
                then 'complete'
            when candidate.next_start_at is not null
                then 'inferred_end_from_next_start'
            else 'missing_end'
        end as interval_quality
    from events as start_event
    inner join start_candidates as candidate
        on start_event.event_key = candidate.start_event_key
),

start_intervals as (
    select
        md5(concat_ws(chr(31), event_key, coalesce(cast(ended_at as varchar), ''))) as interval_key,
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
        duplicate_occurrence_count > 0 as has_duplicate_source,
        event_key as start_event_key,
        null::varchar as end_event_key
    from paired_starts
),

matched_ends as (
    select
        start_interval.device_key,
        start_interval.source_stream,
        start_interval.bundle_id,
        start_interval.ended_at,
        min(end_event.event_key) as end_event_key
    from start_intervals as start_interval
    inner join events as end_event
        on start_interval.device_key = end_event.device_key
        and start_interval.source_stream = end_event.source_stream
        and start_interval.bundle_id = end_event.bundle_id
        and start_interval.ended_at = end_event.event_at
        and end_event.state = 'end'
    where start_interval.quality = 'complete'
    group by all
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
        on end_event.device_key = matched_ends.device_key
        and end_event.source_stream = matched_ends.source_stream
        and end_event.bundle_id = matched_ends.bundle_id
        and end_event.event_at = matched_ends.ended_at
        and end_event.event_key = matched_ends.end_event_key
    where end_event.state = 'end'
      and matched_ends.end_event_key is null
)

select * from start_intervals
union all by name
select * from unmatched_end_intervals
