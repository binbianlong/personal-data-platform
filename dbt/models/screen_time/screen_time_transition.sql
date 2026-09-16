select * exclude (is_active, loaded_at)
from {{ source('screen_time_base', 'screen_time_event') }}
where is_active
