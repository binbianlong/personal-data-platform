variable "fitbit_subject_key" {
  description = "Stable pseudonymous owner key for Fitbit health data."
  type        = string
  default     = ""
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,128}$", var.fitbit_subject_key))
    error_message = "Fitbit ingestion requires a stable pseudonymous subject key."
  }
}

variable "fitbit_delivery_mode" {
  description = "Fitbit notification delivery: push for immediate delivery or pull for scheduled batches."
  type        = string
  default     = "push"
  validation {
    condition     = contains(["push", "pull"], var.fitbit_delivery_mode)
    error_message = "fitbit_delivery_mode must be push or pull."
  }
}

resource "google_cloud_scheduler_job" "fitbit_batch" {
  project          = var.project_id
  region           = var.region
  name             = "pdp-fitbit-batch-west"
  schedule         = "0 * * * *"
  time_zone        = var.scheduler_time_zone
  paused           = !var.schedulers_enabled || var.fitbit_delivery_mode != "pull"
  attempt_deadline = "540s"
  retry_config { retry_count = 0 }
  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.fitbit_worker.uri}/notifications/fitbit/drain"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = google_cloud_run_v2_service.fitbit_worker.uri
    }
  }
  depends_on = [google_cloud_run_v2_service_iam_member.fitbit_worker, google_service_account_iam_member.scheduler_deployer, google_pubsub_subscription_iam_member.fitbit_worker]
}
