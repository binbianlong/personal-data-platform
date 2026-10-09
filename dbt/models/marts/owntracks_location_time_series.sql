{{ config(tags=['owntracks']) }}
select *,
    event_at at time zone 'Asia/Tokyo' as event_at_jst,
    cast(event_at at time zone 'Asia/Tokyo' as date) as activity_date
from {{ source('owntracks_base', 'owntracks_location') }}
