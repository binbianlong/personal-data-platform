locals {
  ingestion_pipelines = merge({
    screen_time_app_in_focus = {
      source_id               = "screen_time"
      stream                  = "app-in-focus"
      raw_prefixes            = ["raw/screen_time/v1/", "raw/screen_time/v2/"]
      raw_suffixes            = [".segb.gz"]
      retention_days          = 90
      lifecycle_grace_days    = 3
      loader_schedule         = var.loader_schedule
      reconciliation_schedule = var.reconciliation_schedule
      raw_creator_members     = ["serviceAccount:${google_service_account.collector.email}"]
    }
  }, var.additional_ingestion_pipelines)

  ingestion_jobs = merge([
    for key, pipeline in local.ingestion_pipelines : {
      for role in ["loader", "reconciliation"] :
      (key == "screen_time_app_in_focus" ? role : "${key}_${role}") => {
        source_id   = pipeline.source_id
        stream      = pipeline.stream
        name        = key == "screen_time_app_in_focus" ? (role == "loader" ? "screen-time-loader" : "reconciliation") : "pdp-${replace(key, "_", "-")}-${role == "loader" ? "load" : "audit"}"
        role        = role
        args        = key == "screen_time_app_in_focus" ? [role, "--source", pipeline.source_id, "--all-streams"] : [role, "--source", pipeline.source_id, "--stream", pipeline.stream]
        timeout     = role == "loader" ? "7200s" : "9000s"
        max_retries = role == "loader" ? 0 : 1
        resources = {
          cpu    = "1"
          memory = "1Gi"
        }
        scheduler_name = key == "screen_time_app_in_focus" ? (role == "loader" ? "screen-time-loader-hourly" : "reconciliation-daily") : "pdp-${replace(key, "_", "-")}-${role}"
        schedule       = role == "loader" ? pipeline.loader_schedule : pipeline.reconciliation_schedule
        environment = merge({
          APP_ENV                  = "production"
          GCS_BUCKET               = local.raw_bucket_name
          GOOGLE_CLOUD_PROJECT     = var.project_id
          MOTHERDUCK_DATABASE      = var.motherduck_database
          PDP_RAW_RETENTION_DAYS   = tostring(pipeline.retention_days)
          PDP_LIFECYCLE_GRACE_DAYS = tostring(pipeline.lifecycle_grace_days)
          PDP_RAW_PREFIXES_JSON    = jsonencode(pipeline.raw_prefixes)
          PDP_RAW_SUFFIXES_JSON    = jsonencode(pipeline.raw_suffixes)
          }, role == "reconciliation" && key == "screen_time_app_in_focus" ? {
          PDP_RECONCILIATION_MONITORING_MODE = "cloud_monitoring"
        } : {})
        secrets = merge({ MOTHERDUCK_TOKEN = "motherduck_token" }, role == "reconciliation" && key != "screen_time_app_in_focus" ? {
          RECONCILIATION_HEARTBEAT_URL = "${key}_heartbeat"
        } : {})
      }
    }
  ]...)

  raw_lifecycle_scopes = flatten([
    for pipeline in values(local.ingestion_pipelines) : [
      for prefix in pipeline.raw_prefixes : [
        for suffix in pipeline.raw_suffixes : {
          prefix         = prefix
          suffix         = suffix
          retention_days = pipeline.retention_days
        }
      ]
    ]
  ])

  compatible_raw_retention = alltrue([
    for pair in setproduct(local.raw_lifecycle_scopes, local.raw_lifecycle_scopes) :
    pair[0].retention_days == pair[1].retention_days ||
    !(startswith(pair[0].prefix, pair[1].prefix) || startswith(pair[1].prefix, pair[0].prefix)) ||
    !(endswith(pair[0].suffix, pair[1].suffix) || endswith(pair[1].suffix, pair[0].suffix))
  ])
}

variable "additional_ingestion_pipelines" {
  description = "Registered source/stream pipelines; enable only after compatible runtime deployment."
  type = map(object({
    source_id               = string
    stream                  = string
    raw_prefixes            = list(string)
    raw_suffixes            = list(string)
    retention_days          = number
    lifecycle_grace_days    = number
    loader_schedule         = string
    reconciliation_schedule = string
    raw_creator_members     = set(string)
  }))
  default = {}

  validation {
    condition = alltrue([
      for key, pipeline in var.additional_ingestion_pipelines :
      can(regex("^[a-z][a-z0-9_]{0,15}$", key)) && key != "screen_time_app_in_focus" &&
      can(regex("^[a-z][a-z0-9_]*$", pipeline.source_id)) &&
      can(regex("^[a-z0-9-]+$", pipeline.stream)) &&
      length(pipeline.raw_prefixes) > 0 && length(pipeline.raw_suffixes) > 0 &&
      alltrue([for prefix in pipeline.raw_prefixes : can(regex("^raw/${pipeline.source_id}/v[1-9][0-9]*/([a-z0-9_-]+/)*$", prefix))]) &&
      alltrue([for suffix in pipeline.raw_suffixes : can(regex("^\\.[a-z0-9-]+\\.gz$", suffix))]) &&
      length(distinct(pipeline.raw_suffixes)) == length(pipeline.raw_suffixes) &&
      alltrue(flatten([
        for index, prefix in pipeline.raw_prefixes : [
          for other_index, other in pipeline.raw_prefixes :
          index == other_index || !startswith(prefix, other)
        ]
      ])) &&
      pipeline.retention_days >= 1 && floor(pipeline.retention_days) == pipeline.retention_days &&
      pipeline.lifecycle_grace_days >= 0 && floor(pipeline.lifecycle_grace_days) == pipeline.lifecycle_grace_days
    ])
    error_message = "Additional pipelines must have distinct short keys, canonical Raw namespaces and gzip suffixes, and integral retention periods."
  }
}
