moved {
  from = google_storage_bucket.raw_west
  to   = google_storage_bucket.raw
}

moved {
  from = google_storage_bucket.preflight_west
  to   = google_storage_bucket.preflight
}

moved {
  from = google_service_account.west
  to   = google_service_account.processing
}

moved {
  from = google_service_account_iam_member.west_deployer
  to   = google_service_account_iam_member.processing_deployer
}

moved {
  from = google_secret_manager_secret_iam_member.west_job
  to   = google_secret_manager_secret_iam_member.jobs
}

moved {
  from = google_cloud_run_v2_job.west
  to   = google_cloud_run_v2_job.runtime
}

moved {
  from = google_service_account.west_receiver
  to   = google_service_account.fitbit_receiver
}

moved {
  from = google_service_account_iam_member.west_receiver_deployer
  to   = google_service_account_iam_member.fitbit_receiver_deployer
}

moved {
  from = google_secret_manager_secret_iam_member.west_receiver
  to   = google_secret_manager_secret_iam_member.fitbit_receiver
}

moved {
  from = google_cloud_run_v2_service.west
  to   = google_cloud_run_v2_service.fitbit_receiver
}

moved {
  from = google_cloud_run_v2_service_iam_member.west_public
  to   = google_cloud_run_v2_service_iam_member.fitbit_public
}

moved {
  from = google_service_account.west_scheduler
  to   = google_service_account.scheduler
}

moved {
  from = google_service_account_iam_member.west_scheduler_deployer
  to   = google_service_account_iam_member.scheduler_deployer
}

moved {
  from = google_cloud_run_v2_job_iam_member.west_scheduler
  to   = google_cloud_run_v2_job_iam_member.scheduler
}

moved {
  from = google_cloud_scheduler_job.west
  to   = google_cloud_scheduler_job.runtime
}

moved {
  from = google_storage_bucket_iam_policy.raw_west
  to   = google_storage_bucket_iam_policy.raw
}

moved {
  from = google_storage_bucket_iam_policy.preflight_west
  to   = google_storage_bucket_iam_policy.preflight
}

moved {
  from = google_logging_project_bucket_config.west
  to   = google_logging_project_bucket_config.runtime
}

moved {
  from = google_logging_project_sink.default_west
  to   = google_logging_project_sink.default
}

moved {
  from = google_monitoring_alert_policy.west_job_failed
  to   = google_monitoring_alert_policy.job_failed
}

moved {
  from = google_monitoring_alert_policy.west_pubsub_backlog
  to   = google_monitoring_alert_policy.pubsub_backlog
}

moved {
  from = google_monitoring_alert_policy.west_receiver_failure
  to   = google_monitoring_alert_policy.receiver_failure
}

moved {
  from = google_secret_manager_secret.west
  to   = google_secret_manager_secret.runtime
}

moved {
  from = google_pubsub_topic.west
  to   = google_pubsub_topic.fitbit
}

moved {
  from = google_pubsub_subscription.west
  to   = google_pubsub_subscription.fitbit
}

moved {
  from = google_pubsub_topic_iam_member.west_receiver
  to   = google_pubsub_topic_iam_member.fitbit_receiver
}
