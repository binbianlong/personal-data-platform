variable "enable_fitbit_runtime" {
  description = "Provision the Fitbit receiver and queue after isolated validation."
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
  description = "Retain notifications while pausing API/warehouse work until the usage gate is satisfied."
  type        = bool
  default     = true
}

variable "fitbit_secret_ids" {
  description = "Existing Secret Manager IDs, keyed by runtime environment name; never secret values."
  type        = map(string)
  default     = {}
  validation {
    condition = !var.enable_fitbit_runtime || toset(keys(var.fitbit_secret_ids)) == toset([
      "PDP_FITBIT_OAUTH_CLIENT_ID", "PDP_FITBIT_OAUTH_CLIENT_SECRET",
      "PDP_FITBIT_OAUTH_REFRESH_TOKEN", "PDP_FITBIT_HEALTH_USER_ID",
      "PDP_FITBIT_WEBHOOK_AUTHORIZATION",
    ])
    error_message = "Provide existing IDs for the five Fitbit credential/owner secrets."
  }
  validation {
    condition     = alltrue([for value in values(var.fitbit_secret_ids) : can(regex("^[A-Za-z0-9_-]+$", value))])
    error_message = "Secret references must be Secret Manager IDs in this project."
  }
}

data "google_project" "fitbit" {
  count      = var.enable_fitbit_runtime ? 1 : 0
  project_id = var.project_id
}

locals {
  fitbit_service_url = var.enable_fitbit_runtime ? "https://pdp-fitbit-${data.google_project.fitbit[0].number}.${var.region}.run.app" : ""
  fitbit_environment = var.enable_fitbit_runtime ? {
    PDP_FITBIT_SUBJECT_KEY          = var.fitbit_subject_key
    PDP_FITBIT_SERVICE_URL          = local.fitbit_service_url
    PDP_FITBIT_TASK_SERVICE_ACCOUNT = "pdp-fitbit-task@${var.project_id}.iam.gserviceaccount.com"
    PDP_FITBIT_TASKS_PARENT         = "projects/${var.project_id}/locations/${var.region}/queues/pdp-fitbit"
    PDP_FITBIT_PROCESSING_PAUSED    = tostring(var.fitbit_processing_paused)
    PDP_FITBIT_REPAIR_ENABLED       = "true"
  } : {}
  fitbit_storage_members = var.enable_fitbit_runtime ? [
    "serviceAccount:pdp-fitbit@${var.project_id}.iam.gserviceaccount.com",
    "serviceAccount:reconciliation@${var.project_id}.iam.gserviceaccount.com",
  ] : []
  fitbit_service_secrets = var.enable_fitbit_runtime ? merge(var.fitbit_secret_ids, {
    MOTHERDUCK_TOKEN = google_secret_manager_secret.runtime["motherduck_token"].secret_id
  }) : {}
  fitbit_reconciliation_secrets = var.enable_fitbit_runtime ? {
    for key in [
      "PDP_FITBIT_OAUTH_CLIENT_ID",
      "PDP_FITBIT_OAUTH_CLIENT_SECRET",
      "PDP_FITBIT_OAUTH_REFRESH_TOKEN",
      "PDP_FITBIT_HEALTH_USER_ID",
    ] : key => var.fitbit_secret_ids[key]
  } : {}
}

resource "google_service_account" "fitbit" {
  count        = var.enable_fitbit_runtime ? 1 : 0
  project      = var.project_id
  account_id   = "pdp-fitbit"
  display_name = "PDP Fitbit receiver and worker"
  depends_on   = [google_project_service.runtime]
}

resource "google_service_account" "fitbit_task" {
  count        = var.enable_fitbit_runtime ? 1 : 0
  project      = var.project_id
  account_id   = "pdp-fitbit-task"
  display_name = "PDP Fitbit task caller"
  depends_on   = [google_project_service.runtime]
}

resource "google_service_account_iam_member" "fitbit_deployer" {
  for_each           = var.enable_fitbit_runtime ? toset(["pdp-fitbit", "pdp-fitbit-task"]) : toset([])
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.key}@${var.project_id}.iam.gserviceaccount.com"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
  depends_on         = [google_service_account.fitbit, google_service_account.fitbit_task]
}

resource "google_service_account_iam_member" "fitbit_task_creator" {
  for_each           = toset(local.fitbit_storage_members)
  service_account_id = google_service_account.fitbit_task[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
  depends_on         = [google_service_account.fitbit, google_service_account.runtime]
}

resource "google_service_account_iam_member" "fitbit_task_token" {
  count              = var.enable_fitbit_runtime ? 1 : 0
  service_account_id = google_service_account.fitbit_task[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.fitbit[0].number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
  depends_on         = [google_cloud_tasks_queue.fitbit]
}

resource "google_secret_manager_secret_iam_member" "fitbit" {
  for_each  = local.fitbit_service_secrets
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.fitbit[0].email}"
}

resource "google_secret_manager_secret_iam_member" "fitbit_reconciliation" {
  for_each  = local.fitbit_reconciliation_secrets
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime["reconciliation"].email}"
}

resource "google_cloud_tasks_queue" "fitbit" {
  count    = var.enable_fitbit_runtime ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = "pdp-fitbit"
  rate_limits {
    max_concurrent_dispatches = 1
    max_dispatches_per_second = 1
  }
  retry_config {
    max_attempts       = 20
    max_retry_duration = "86400s"
    min_backoff        = "10s"
    max_backoff        = "3600s"
    max_doublings      = 8
  }
  depends_on = [google_project_service.runtime]
}

resource "google_cloud_tasks_queue_iam_member" "fitbit" {
  for_each   = toset(local.fitbit_storage_members)
  project    = var.project_id
  location   = var.region
  name       = google_cloud_tasks_queue.fitbit[0].name
  role       = "roles/cloudtasks.enqueuer"
  member     = each.value
  depends_on = [google_service_account.fitbit, google_service_account.runtime]
}

resource "google_cloud_run_v2_service" "fitbit" {
  count               = var.enable_fitbit_runtime ? 1 : 0
  project             = var.project_id
  location            = var.region
  name                = "pdp-fitbit"
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  template {
    service_account                  = google_service_account.fitbit[0].email
    timeout                          = "1800s"
    max_instance_request_concurrency = 16
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = var.image_uri
      args  = ["fitbit", "serve"]
      resources {
        limits   = { cpu = "1", memory = "1Gi" }
        cpu_idle = true
      }
      dynamic "env" {
        for_each = merge(local.fitbit_environment, {
          GOOGLE_CLOUD_PROJECT = var.project_id
          GCS_BUCKET           = local.raw_bucket_name
          MOTHERDUCK_DATABASE  = var.motherduck_database
        })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.fitbit_service_secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.fitbit, google_service_account_iam_member.fitbit_deployer]
}

resource "google_cloud_run_v2_service_iam_member" "fitbit_public" {
  count    = var.enable_fitbit_runtime ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.fitbit[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_monitoring_alert_policy" "fitbit_failure" {
  count        = var.enable_fitbit_runtime ? 1 : 0
  project      = var.project_id
  display_name = "Fitbit delivery or processing failure"
  combiner     = "OR"
  conditions {
    display_name = "Fitbit failure log"
    condition_matched_log {
      filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"pdp-fitbit\" AND (textPayload=~\"fitbit (webhook|task) failed\" OR jsonPayload.message=~\"fitbit (webhook|task) failed\")"
    }
  }
  alert_strategy {
    notification_rate_limit { period = "3600s" }
    auto_close = "86400s"
  }
  notification_channels = [google_monitoring_notification_channel.email.name]
}

resource "google_logging_metric" "fitbit_repair_success" {
  count       = var.enable_fitbit_runtime ? 1 : 0
  project     = var.project_id
  name        = "pdp-fitbit-repair-success"
  description = "Complete bounded Fitbit repair passes, excluding paused, deferred and failed results."
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.project_id=\"${var.project_id}\"",
    "resource.labels.location=\"${var.region}\"",
    "resource.labels.job_name=\"reconciliation\"",
    "jsonPayload.event=\"fitbit_repair\"",
    "jsonPayload.status=\"succeeded\"",
  ])
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [google_project_service.runtime]
}

resource "google_monitoring_alert_policy" "fitbit_repair_absent" {
  count        = var.enable_fitbit_runtime ? 1 : 0
  project      = var.project_id
  display_name = "Fitbit repair: successful completion absent"
  combiner     = "OR"
  enabled      = !var.fitbit_processing_paused
  conditions {
    display_name = "No completed Fitbit repair pass for approximately one day"
    condition_absent {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "resource.labels.project_id = \"${var.project_id}\"",
        "resource.labels.location = \"${var.region}\"",
        "resource.labels.job_name = \"reconciliation\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.fitbit_repair_success[0].name}\"",
      ])
      duration = "84600s"
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger { count = 1 }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.name]
  documentation {
    mime_type = "text/markdown"
    content   = "No successful Fitbit repair pass has been observed for approximately one day. Inspect deferred_phases, receipt_read_deferred_count, shared leases and scheduled executions. Succeeded means the bounded pass completed, not that all queued receipts have finished. After enabling, unpausing or modifying this policy, confirm a fresh success metric point before relying on absence detection."
  }
  depends_on = [google_project_service.runtime]
}

output "fitbit_service_url" {
  description = "Receiver URL; verify authentication before registering subscriptions."
  value       = var.enable_fitbit_runtime ? "${local.fitbit_service_url}/webhooks/fitbit" : null
}
