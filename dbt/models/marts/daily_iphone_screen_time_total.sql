{{ config(tags=['iphone_screen_time']) }}
{{ screen_time_total(ref('iphone_screen_time_interval').include(database=false), source('base', 'iphone_screen_time_event').include(database=false)) }}
