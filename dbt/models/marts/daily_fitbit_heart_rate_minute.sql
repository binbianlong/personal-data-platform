{{ config(tags=['fitbit']) }}
select subject_key, cast(timezone('Asia/Tokyo', start_at) as date) as activity_date,
    avg(mean_minute_heart_rate) as mean_minute_heart_rate,
    min(min_heart_rate) as min_heart_rate,
    max(max_heart_rate) as max_heart_rate,
    count(*) as observed_heart_rate_minutes
from {{ ref('fitbit_heart_rate_minute_time_series') }}
group by subject_key, activity_date
