locals {
  scheduled_jobs = {
    loader = {
      name        = "screen-time-loader-hourly"
      schedule    = var.loader_schedule
      description = "Run the Screen Time loader at minute 15 of every hour"
    }
    reconciliation = {
      name        = "reconciliation-daily"
      schedule    = var.reconciliation_schedule
      description = "Run full reconciliation every day at 04:30 Asia/Tokyo"
    }
  }
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "scheduler-invoker"
  display_name = "Cloud Scheduler invoker"
  description  = "Invokes only the loader and reconciliation Cloud Run Jobs"

  depends_on = [google_project_service.runtime]
}

resource "google_service_account_iam_member" "deployer_act_as_scheduler" {
  service_account_id = google_service_account.scheduler.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler" {
  for_each = local.scheduled_jobs

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.runtime[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "runtime" {
  for_each = local.scheduled_jobs

  project          = var.project_id
  region           = var.region
  name             = each.value.name
  description      = each.value.description
  schedule         = each.value.schedule
  time_zone        = var.scheduler_time_zone
  attempt_deadline = "320s"

  retry_config {
    retry_count          = 3
    max_retry_duration   = "3600s"
    min_backoff_duration = "5s"
    max_backoff_duration = "900s"
    max_doublings        = 5
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.runtime[each.key].name}:run"
    body        = base64encode("{}")

    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_cloud_run_v2_job_iam_member.scheduler,
    google_project_service.runtime,
    google_service_account_iam_member.deployer_act_as_scheduler,
  ]
}
