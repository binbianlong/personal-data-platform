{{ config(tags=['fitbit']) }}
with parts as (
 select g.day::date as activity_date, i.*, 
  i.active_zone_minutes * epoch(least(i.end_at, (g.day::timestamp + interval '1 day') at time zone 'Asia/Tokyo') - greatest(i.start_at, g.day::timestamp at time zone 'Asia/Tokyo')) / epoch(i.end_at-i.start_at) as part_value,
  (i.start_at at time zone 'Asia/Tokyo')::date != ((i.end_at - interval '1 microsecond') at time zone 'Asia/Tokyo')::date as prorated
 from {{ source('base', 'active_zone_interval').include(database=false) }} i cross join lateral generate_series(
 date_trunc('day', i.start_at at time zone 'Asia/Tokyo'), date_trunc('day', (i.end_at - interval '1 microsecond') at time zone 'Asia/Tokyo'), interval '1 day') g(day)
)
select activity_date, sum(part_value)::double as active_zone_minutes,
sum(part_value) filter(where lower(category) = 'fat_burn')::double as fat_burn_azm_minutes,
sum(part_value) filter(where lower(category) = 'cardio')::double as cardio_azm_minutes,
sum(part_value) filter(where lower(category) = 'peak')::double as peak_azm_minutes,
sum(part_value) filter(where lower(category) = 'heart_rate_zone_unspecified')::double as unspecified_azm_minutes,
 count(*)::bigint as interval_part_count, bool_or(prorated) as has_prorated_intervals
from parts group by activity_date
