variable "enable_fitbit_runtime" {
  description = "Enable Fitbit acquisition in the shared daily reconciliation Job."
  type        = bool
  default     = false
}

variable "fitbit_subject_key" {
  description = "Stable pseudonymous owner key for Fitbit health data."
  type        = string
  default     = ""
  validation {
    condition     = !var.enable_fitbit_runtime || can(regex("^[A-Za-z0-9_-]{1,128}$", var.fitbit_subject_key))
    error_message = "Enabled Fitbit ingestion requires a stable pseudonymous subject key."
  }
}

variable "fitbit_processing_paused" {
  description = "Pause Fitbit acquisition without changing the shared daily schedule."
  type        = bool
  default     = true
}

variable "fitbit_secret_ids" {
  description = "Existing OAuth Secret Manager IDs, keyed by runtime environment name."
  type        = map(string)
  default     = {}
  validation {
    condition = !var.enable_fitbit_runtime || (
      toset(keys(var.fitbit_secret_ids)) == toset(["PDP_FITBIT_OAUTH_CREDENTIALS"]) ||
      toset(keys(var.fitbit_secret_ids)) == toset([
        "PDP_FITBIT_OAUTH_CLIENT_ID", "PDP_FITBIT_OAUTH_CLIENT_SECRET", "PDP_FITBIT_OAUTH_REFRESH_TOKEN",
      ])
    )
    error_message = "Provide one OAuth bundle secret ID or three separate OAuth secret IDs."
  }
  validation {
    condition     = alltrue([for value in values(var.fitbit_secret_ids) : can(regex("^[A-Za-z0-9_-]+$", value))])
    error_message = "Secret references must be Secret Manager IDs in this project."
  }
}

locals {
  fitbit_environment = var.enable_fitbit_runtime ? {
    PDP_FITBIT_SUBJECT_KEY       = var.fitbit_subject_key
    PDP_FITBIT_PROCESSING_PAUSED = tostring(var.fitbit_processing_paused)
    PDP_FITBIT_DAILY_ENABLED     = "true"
  } : {}
  fitbit_storage_members = var.enable_fitbit_runtime ? [
    "serviceAccount:reconciliation@${var.project_id}.iam.gserviceaccount.com",
  ] : []
  fitbit_reconciliation_secrets = var.enable_fitbit_runtime ? var.fitbit_secret_ids : {}
}

resource "google_secret_manager_secret_iam_member" "fitbit_reconciliation" {
  for_each  = local.fitbit_reconciliation_secrets
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime["reconciliation"].email}"
}

resource "google_logging_metric" "fitbit_daily_success" {
  count       = var.enable_fitbit_runtime ? 1 : 0
  project     = var.project_id
  name        = "pdp-fitbit-daily-success"
  description = "Complete bounded Fitbit daily collection passes, excluding paused, deferred and failed results."
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.project_id=\"${var.project_id}\"",
    "resource.labels.location=\"${var.region}\"",
    "resource.labels.job_name=\"reconciliation\"",
    "jsonPayload.event=\"fitbit_daily\"",
    "jsonPayload.status=\"succeeded\"",
  ])
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [google_project_service.runtime]
}

resource "google_logging_metric" "fitbit_daily_age" {
  count       = var.enable_fitbit_runtime ? 1 : 0
  project     = var.project_id
  name        = "pdp-fitbit-daily-success-age"
  description = "Seconds since a fully completed Fitbit daily collection pass, or its first attempt before any success."
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.project_id=\"${var.project_id}\"",
    "resource.labels.location=\"${var.region}\"",
    "resource.labels.job_name=\"reconciliation\"",
    "jsonPayload.event=\"fitbit_daily\"",
    "jsonPayload.summary.full_success_age_seconds>=0",
  ])
  value_extractor = "EXTRACT(jsonPayload.summary.full_success_age_seconds)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
  }
  bucket_options {
    explicit_buckets { bounds = [0, 86400, 172800, 604800] }
  }
  depends_on = [google_project_service.runtime]
}

resource "google_monitoring_alert_policy" "fitbit_daily_absent" {
  count        = var.enable_fitbit_runtime ? 1 : 0
  project      = var.project_id
  display_name = "Fitbit daily collection: successful completion absent"
  combiner     = "OR"
  enabled      = !var.fitbit_processing_paused
  conditions {
    display_name = "Full Fitbit daily collection pass overdue by more than 48 hours"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "resource.labels.project_id = \"${var.project_id}\"",
        "resource.labels.location = \"${var.region}\"",
        "resource.labels.job_name = \"reconciliation\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.fitbit_daily_age[0].name}\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 172800
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_MEAN"
      }
      trigger { count = 1 }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.name]
  documentation {
    mime_type = "text/markdown"
    content   = "The daily Fitbit collector reports more than 48 hours since its last complete pass, or its first attempt if none succeeded. Deferred and failed runs preserve that clock. Daily sampling can delay detection until the next run, approaching 72 hours. Inspect shared leases, upload intents and scheduled executions. Missing executions are covered by the reconciliation policy."
  }
  depends_on = [google_project_service.runtime]
}
