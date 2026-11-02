{{ config(tags=['mac_screen_time']) }}
{{ screen_time_total(ref('mac_screen_time_interval').include(database=false), source('base', 'mac_screen_time_event').include(database=false)) }}
