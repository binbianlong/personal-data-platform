select * exclude (is_active, loaded_at)
from {{ source('iphone_screen_time_base', 'iphone_screen_time_event') }}
where is_active
