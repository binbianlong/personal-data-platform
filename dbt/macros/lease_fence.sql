{% macro lease_fence() %}
{% set owner = env_var('PDP_DBT_LEASE_OWNER', '') %}
{% if target.name == 'prod' and not owner %}
  select error('Production dbt requires a loader lease owner')
{% elif owner %}
  {% set escaped = owner | replace("'", "''") %}
  update ops.job_lock set expires_at = expires_at - interval '1 microsecond'
  where job_name = 'loader' and owner_id = '{{ escaped }}'
    and expires_at > current_timestamp + interval '1 microsecond';
  select case when exists (select 1 from ops.job_lock where job_name = 'loader'
    and owner_id = '{{ escaped }}' and expires_at > current_timestamp + interval '1 microsecond')
    then 1 else error('loader lease lost') end
{% else %}
  select 1
{% endif %}
{% endmacro %}
