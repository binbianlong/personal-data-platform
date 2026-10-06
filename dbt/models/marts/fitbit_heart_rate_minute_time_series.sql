{{ config(tags=['fitbit']) }}
select subject_key, data_source_family, start_at, end_at,
    average as mean_minute_heart_rate, minimum as min_heart_rate,
    maximum as max_heart_rate, sample_count, origin, aggregation_version,
    fetched_at, source_key
from {{ source('fitbit_base', 'fitbit_heart_rate_minute') }}
