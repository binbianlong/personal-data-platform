select * from {{ ref('fitbit_sleep_mac_screen_time') }}
where screen_time_seconds < 0 or screen_time_seconds > 7200
