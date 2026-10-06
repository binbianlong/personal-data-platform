{{ config(tags=['fitbit']) }}
select * from {{ ref('fitbit_heart_rate_minute_time_series') }}
