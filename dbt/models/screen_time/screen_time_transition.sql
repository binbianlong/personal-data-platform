select legacy.*
from {{ ref('screen_time_legacy_transition') }} as legacy
where not exists (
    select 1 from {{ source('screen_time_base', 'screen_time_event') }} as event
    where event.event_key = legacy.event_key
)
union all
select * exclude (is_active, loaded_at)
from {{ source('screen_time_base', 'screen_time_event') }}
where is_active
