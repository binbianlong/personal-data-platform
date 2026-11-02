{{ config(tags=['mac_screen_time']) }}
{{ screen_time_intervals(source('base', 'mac_screen_time_event').include(database=false)) }}
