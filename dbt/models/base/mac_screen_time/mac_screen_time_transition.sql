select * exclude (is_active, loaded_at)
from {{ source('mac_screen_time_base', 'mac_screen_time_event') }}
where is_active
