output "runtime_jobs" {
  description = "Cloud Run Job names by logical role."
  value = {
    for key, job in google_cloud_run_v2_job.runtime : key => job.name
  }
}

output "runtime_service_accounts" {
  description = "Dedicated Service Account email addresses by logical role."
  value = {
    for key, account in google_service_account.runtime : key => account.email
  }
}

output "collector_service_account" {
  description = "Dedicated Service Account impersonated by the Mac Collector."
  value       = google_service_account.collector.email
}

output "rebuild_operator_service_account" {
  description = "Read-only Service Account impersonated for explicit local Raw rebuilds."
  value       = google_service_account.rebuild_operator.email
}

output "storage_buckets" {
  description = "Production Raw and isolated preflight bucket names."
  value = {
    raw       = google_storage_bucket.raw.name
    preflight = google_storage_bucket.preflight.name
  }
}

output "scheduler_jobs" {
  description = "Cloud Scheduler job names and schedules."
  value = {
    for key, job in local.scheduled_jobs : key => {
      name      = job.name
      schedule  = job.schedule
      time_zone = var.scheduler_time_zone
    }
  }
}

output "secret_ids" {
  description = "Secret Manager resources that require out-of-band secret versions."
  value = {
    for key in keys(local.runtime_secrets) : key => google_secret_manager_secret.runtime[key].secret_id
  }
}

output "notification_channel" {
  description = "Cloud Monitoring email notification channel."
  value       = google_monitoring_notification_channel.email.name
}
