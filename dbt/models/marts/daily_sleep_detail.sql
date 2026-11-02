{{ config(tags=['fitbit']) }}
with intervals as (
 select (s.end_at at time zone 'Asia/Tokyo')::date as sleep_date, d.kind, d.category, d.start_at, d.end_at
 from {{ source('base','sleep_detail').include(database=false) }} d join {{ source('base','sleep_session').include(database=false) }} s using(sleep_session_id)
), previous as (
 select *, max(end_at) over(partition by sleep_date, kind, category order by start_at, end_at rows between unbounded preceding and 1 preceding) as previous_end from intervals
), grouped as (
 select *, sum(case when previous_end is null or start_at > previous_end then 1 else 0 end) over(partition by sleep_date, kind, category order by start_at, end_at rows unbounded preceding) as grp from previous
), merged as (
 select sleep_date, kind, category, grp, min(start_at) as start_at, max(end_at) as end_at from grouped group by all
)
select sleep_date, kind, category, (sum(epoch(end_at-start_at))/60)::double as total_minutes from merged group by all
