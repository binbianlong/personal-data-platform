{% macro screen_time_intervals(relation) %}
with events as (
  select * from {{ relation }} where is_valid
), next_events as (
 select *,
 first_value(case when state = 'start' then event_key end ignore nulls) over (
   partition by device_key order by event_at, case when state = 'end' then 0 else 1 end, event_key
   rows between 1 following and unbounded following) as next_start_key,
 first_value(case when state = 'end' then event_key end ignore nulls) over (
   partition by device_key, bundle_id order by event_at, case when state = 'end' then 0 else 1 end, event_key
   rows between 1 following and unbounded following) as next_end_key
 from events
), pairs as (
 select s.event_key as start_event_key, s.device_key, s.bundle_id, s.event_at as started_at,
   case when e.event_key is not null and (n.event_key is null or e.event_at <= n.event_at)
     then e.event_at else n.event_at end as ended_at,
   not (e.event_key is not null and (n.event_key is null or e.event_at <= n.event_at)) as is_inferred,
   case when e.event_key is not null and (n.event_key is null or e.event_at <= n.event_at)
     then e.event_key else n.event_key end as boundary_event_key
 from next_events s left join events e on e.event_key=s.next_end_key
 left join events n on n.event_key=s.next_start_key where s.state='start'
)
select start_event_key, device_key, bundle_id, started_at, ended_at,
 epoch(ended_at - started_at)::double as duration_seconds, is_inferred, boundary_event_key
from pairs where ended_at > started_at
{% endmacro %}

{% macro screen_time_total(intervals, events) %}
with days as (
 select g.day::date as activity_date, i.is_inferred,
 greatest(i.started_at, g.day::timestamp at time zone 'Asia/Tokyo') as started_at,
 least(i.ended_at, (g.day::timestamp + interval '1 day') at time zone 'Asia/Tokyo') as ended_at
 from {{ intervals }} i cross join lateral generate_series(
 date_trunc('day', i.started_at at time zone 'Asia/Tokyo'),
 date_trunc('day', (i.ended_at - interval '1 microsecond') at time zone 'Asia/Tokyo'), interval '1 day') g(day)
), edges as (
 select activity_date, started_at as t from days union select activity_date, ended_at from days
), spans as (
 select activity_date, t as started_at, lead(t) over(partition by activity_date order by t) as ended_at from edges
), coverage as (
 select s.activity_date, epoch(s.ended_at - s.started_at) as seconds,
 bool_or(not d.is_inferred) as observed
 from spans s join days d on d.activity_date=s.activity_date and d.started_at < s.ended_at and d.ended_at > s.started_at
 where s.ended_at > s.started_at group by s.activity_date, s.started_at, s.ended_at
), totals as (
 select activity_date, sum(seconds) filter(where observed) as observed,
 sum(seconds) as total from coverage group by activity_date
), unresolved as (
 select (e.event_at at time zone 'Asia/Tokyo')::date as activity_date, count(*) as n
 from {{ events }} e where e.is_valid and not exists (
   select 1 from {{ intervals }} i where (e.state='start' and i.start_event_key=e.event_key)
      or (e.state='end' and not i.is_inferred and i.boundary_event_key=e.event_key)
 ) group by 1
)
select coalesce(t.activity_date,u.activity_date) as activity_date,
 case when t.total is not null then coalesce(t.observed,0)::double end as observed_seconds,
 case when t.total is not null then (t.total-coalesce(t.observed,0))::double end as inferred_seconds,
 t.total::double as total_seconds, coalesce(u.n,0)::bigint as unresolved_event_count
from totals t full join unresolved u using(activity_date)
{% endmacro %}
