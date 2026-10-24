select *
from {{ ref('screen_time_interval') }}
where duration_seconds < 0
   or (
       quality in ('complete', 'inferred_end_from_next_start')
       and (started_at is null or ended_at is null or duration_seconds is null)
   )
