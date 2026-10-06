output "runtime_jobs" {
  description = "Cloud Run Job names by logical role."
  value = {
    for key, job in google_cloud_run_v2_job.west : key => job.name
  }
}

output "runtime_service_accounts" {
  description = "Shared processing Service Account email address."
  value = {
    for key, account in google_service_account.west : key => account.email
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
  description = "Retained legacy backup bucket names; normal storage is in west_resources."
  value = {
    raw       = google_storage_bucket.raw.name
    preflight = google_storage_bucket.preflight.name
  }
}

output "scheduler_jobs" {
  description = "Cloud Scheduler job names and schedules."
  value = {
    for key, job in google_cloud_scheduler_job.west : key => {
      name      = job.name
      schedule  = job.schedule
      time_zone = var.scheduler_time_zone
    }
  }
}

output "secret_ids" {
  description = "Secret Manager resources that require out-of-band secret versions."
  value = {
    for key, secret in google_secret_manager_secret.west : key => secret.secret_id
  }
}

output "notification_channel" {
  description = "Cloud Monitoring email notification channel."
  value       = google_monitoring_notification_channel.email.name
}

output "west_resources" {
  description = "Normal west resources and cutover state."
  value = var.enable_west_runtime ? {
    region              = var.west_region
    raw_bucket          = google_storage_bucket.raw_west[0].name
    preflight_bucket    = google_storage_bucket.preflight_west[0].name
    jobs                = { for key, job in google_cloud_run_v2_job.west : key => job.name }
    receiver_url        = "${google_cloud_run_v2_service.west[0].uri}/webhooks/fitbit"
    secrets             = { for key, secret in google_secret_manager_secret.west : key => secret.secret_id }
    pubsub_topic        = google_pubsub_topic.west[0].id
    pubsub_subscription = google_pubsub_subscription.west[0].id
    pubsub_endpoint     = "pubsub.us-west1.rep.googleapis.com"
    logging_bucket      = google_logging_project_bucket_config.west[0].id
    schedulers_enabled  = var.west_schedulers_enabled
  } : null
}
