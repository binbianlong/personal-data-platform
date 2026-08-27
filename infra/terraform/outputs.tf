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
    for key, secret in google_secret_manager_secret.runtime : key => secret.secret_id
  }
}

output "notification_channel" {
  description = "Cloud Monitoring email notification channel."
  value       = google_monitoring_notification_channel.email.name
}
