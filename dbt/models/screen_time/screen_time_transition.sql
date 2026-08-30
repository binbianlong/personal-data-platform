with ranked_segments as (
    select
        object_key,
        row_number() over (
            partition by device_key, source_stream, segment_key
            order by observed_at desc, object_key desc
        ) as segment_version_rank
    from {{ source('screen_time_base', 'screen_time_segment_observation') }}
),

ranked_record_states as (
    select
        occurrence.*,
        row_number() over (
            partition by occurrence.object_key, occurrence.record_offset
            order by occurrence.record_metadata_offset desc
        ) as record_state_rank
    from {{ source('screen_time_base', 'screen_time_record_occurrence') }} as occurrence
    inner join ranked_segments
        on occurrence.object_key = ranked_segments.object_key
        and ranked_segments.segment_version_rank = 1
),

current_occurrences as (
    select *
    from ranked_record_states
    where record_state_rank = 1
      and upper(record_state) = 'WRITTEN'
      and crc_passed is distinct from false
),

ranked_events as (
    select
        *,
        count(*) over (partition by event_key) as logical_occurrence_count,
        row_number() over (
            partition by event_key
            order by observed_at desc, object_key desc, record_metadata_offset desc
        ) as event_rank
    from current_occurrences
)

select
    event_key,
    device_key,
    'ios' as platform,
    source_stream,
    bundle_id,
    event_at,
    case when in_foreground then 'start' else 'end' end as state,
    transition_reason,
    kind,
    app_version,
    app_build,
    platform_flag,
    object_key,
    segment_key,
    segment_filename,
    record_offset,
    record_metadata_offset,
    observed_at,
    parser_version,
    unknown_field_count,
    logical_occurrence_count - 1 as duplicate_occurrence_count
from ranked_events
where event_rank = 1
