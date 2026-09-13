-- A filename alone or a reused trailer slot is not a deletion identity.
with segment_names as (
    select device_key, source_stream, segment_key, min(source_segment_name) as segment_name
    from {{ source('screen_time_base', 'screen_time_segment_observation') }}
    where segment_kind = 'events' and source_segment_name is not null
    group by device_key, source_stream, segment_key
    having count(distinct source_segment_name) = 1
),

unambiguous_names as (
    select * from segment_names
    qualify count(*) over (partition by device_key, source_stream, segment_name) = 1
),

record_states as (
    select *
    from {{ source('screen_time_base', 'screen_time_record_occurrence') }}
    qualify row_number() over (
        partition by object_key, record_offset order by record_metadata_offset desc
    ) = 1
)

select distinct
    tombstone.object_key as tombstone_object_key,
    tombstone.record_metadata_offset as tombstone_metadata_offset,
    tombstone.deletion_reason,
    event.object_key as event_object_key,
    event.record_metadata_offset as event_metadata_offset,
    event.event_key
from record_states as tombstone
inner join unambiguous_names as identity
    on tombstone.device_key = identity.device_key
    and tombstone.source_stream = identity.source_stream
    and tombstone.target_segment_name = identity.segment_name
inner join record_states as event
    on event.device_key = identity.device_key
    and event.source_stream = identity.source_stream
    and event.segment_key = identity.segment_key
    and event.record_metadata_offset = tombstone.target_offset
    and event.payload_length = tombstone.target_length
    and abs(event.record_timestamp_cocoa - tombstone.target_event_timestamp) <= 0.000001
where tombstone.record_kind = 'tombstone'
    and upper(tombstone.record_state) = 'WRITTEN'
    and tombstone.crc_passed is distinct from false
    and event.record_kind = 'event'
    and upper(event.record_state) = 'WRITTEN'
    and event.crc_passed is distinct from false
    and event.event_key is not null
