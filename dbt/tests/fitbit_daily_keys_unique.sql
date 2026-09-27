{{ config(tags=['fitbit']) }}
select subject_key, activity_date
from {{ ref('daily_fitbit_health') }}
group by subject_key, activity_date having count(*) > 1
