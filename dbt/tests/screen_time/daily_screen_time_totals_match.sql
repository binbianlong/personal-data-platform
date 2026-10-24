with interval_total as (
    select coalesce(sum(duration_seconds), 0) as seconds
    from {{ ref('screen_time_interval') }}
    where quality in ('complete', 'inferred_end_from_next_start')
      and ended_at > started_at
),

daily_total as (
    select coalesce(sum(total_seconds), 0) as seconds
    from {{ ref('daily_screen_time') }}
)

select interval_total.seconds, daily_total.seconds
from interval_total
cross join daily_total
where abs(interval_total.seconds - daily_total.seconds) > 0.000001
