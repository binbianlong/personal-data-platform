output "runtime_jobs" {
  description = "Cloud Run Job names by logical role."
  value = {
    for key, job in google_cloud_run_v2_job.west : key => job.name
  }
}

output "runtime_service_accounts" {
  description = "Service Account email addresses by runtime role."
  value = {
    processing = google_service_account.west.email
    receiver   = google_service_account.west_receiver.email
    scheduler  = google_service_account.west_scheduler.email
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
    raw       = google_storage_bucket.raw_west.name
    preflight = google_storage_bucket.preflight_west.name
  }
}

output "scheduler_jobs" {
  description = "Cloud Scheduler job names and schedules."
  value = {
    for key, job in google_cloud_scheduler_job.west : key => {
      name      = job.name
      schedule  = job.schedule
      time_zone = job.time_zone
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
  description = "Runtime resources and scheduled processing status."
  value = {
    region              = var.west_region
    raw_bucket          = google_storage_bucket.raw_west.name
    preflight_bucket    = google_storage_bucket.preflight_west.name
    jobs                = { for key, job in google_cloud_run_v2_job.west : key => job.name }
    receiver_url        = "${google_cloud_run_v2_service.west.uri}/webhooks/fitbit"
    secrets             = { for key, secret in google_secret_manager_secret.west : key => secret.secret_id }
    pubsub_topic        = google_pubsub_topic.west.id
    pubsub_subscription = google_pubsub_subscription.west.id
    pubsub_endpoint     = "pubsub.us-west1.rep.googleapis.com"
    logging_bucket      = google_logging_project_bucket_config.west.id
    schedulers_enabled  = var.west_schedulers_enabled
  }
}
