select
    tombstone.object_key,
    tombstone.record_metadata_offset,
    tombstone.device_key,
    tombstone.source_stream,
    tombstone.target_segment_name,
    tombstone.target_offset,
    tombstone.target_length,
    tombstone.target_event_timestamp,
    tombstone.deletion_reason,
    count(distinct matched.event_key) as matched_event_count,
    case
        when tombstone.deletion_reason not in (1, 2) then 'unsupported_reason'
        when count(matched.event_key) = 0 then 'unmatched'
        when tombstone.deletion_reason = 1 then 'ttl_history_retained'
        else 'user_deletion_applied'
    end as status
from {{ source('screen_time_base', 'screen_time_record_occurrence') }} as tombstone
left join {{ ref('screen_time_tombstone_match') }} as matched
    on tombstone.object_key = matched.tombstone_object_key
    and tombstone.record_metadata_offset = matched.tombstone_metadata_offset
where tombstone.record_kind = 'tombstone'
    and upper(tombstone.record_state) = 'WRITTEN'
    and tombstone.crc_passed is distinct from false
group by 1, 2, 3, 4, 5, 6, 7, 8, 9
