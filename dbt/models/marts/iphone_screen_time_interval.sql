{{ config(tags=['iphone_screen_time']) }}
{{ screen_time_intervals(source('base', 'iphone_screen_time_event').include(database=false)) }}
