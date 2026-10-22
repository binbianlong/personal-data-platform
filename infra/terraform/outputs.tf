output "runtime_jobs" {
  description = "Cloud Run Job names by logical role."
  value = {
    for key, job in google_cloud_run_v2_job.runtime : key => job.name
  }
}

output "runtime_service_accounts" {
  description = "Service Account email addresses by runtime role."
  value = {
    processing = google_service_account.processing.email
    receiver   = google_service_account.fitbit_receiver.email
    scheduler  = google_service_account.scheduler.email
  }
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
  value = merge(
    { for key, job in google_cloud_scheduler_job.runtime : key => {
      name      = job.name
      schedule  = job.schedule
      time_zone = job.time_zone
    } },
    { fitbit_batch = {
      name      = google_cloud_scheduler_job.fitbit_batch.name
      schedule  = google_cloud_scheduler_job.fitbit_batch.schedule
      time_zone = google_cloud_scheduler_job.fitbit_batch.time_zone
    } }
  )
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

output "runtime_resources" {
  description = "Runtime resources and scheduled processing status."
  value = {
    region                 = var.region
    raw_bucket             = google_storage_bucket.raw.name
    preflight_bucket       = google_storage_bucket.preflight.name
    jobs                   = { for key, job in google_cloud_run_v2_job.runtime : key => job.name }
    receiver_url           = "${google_cloud_run_v2_service.fitbit_receiver.uri}/webhooks/fitbit"
    worker_url             = "${google_cloud_run_v2_service.fitbit_worker.uri}/notifications/fitbit"
    fitbit_drain_url       = "${google_cloud_run_v2_service.fitbit_worker.uri}/notifications/fitbit/drain"
    fitbit_delivery_mode   = var.fitbit_delivery_mode
    secrets                = { for key, secret in google_secret_manager_secret.runtime : key => secret.secret_id }
    pubsub_topic           = google_pubsub_topic.fitbit.id
    pubsub_subscription    = google_pubsub_subscription.fitbit.id
    pubsub_endpoint        = "pubsub.us-west1.rep.googleapis.com"
    owntracks_url          = "${google_cloud_run_v2_service.owntracks["receiver"].uri}/webhooks/owntracks"
    owntracks_worker_url   = "${google_cloud_run_v2_service.owntracks["worker"].uri}/notifications/owntracks"
    owntracks_subscription = google_pubsub_subscription.owntracks.id
    logging_bucket         = google_logging_project_bucket_config.runtime.id
    schedulers_enabled     = var.schedulers_enabled
  }
}
