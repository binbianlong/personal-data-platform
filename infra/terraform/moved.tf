moved {
  from = google_pubsub_topic.west[0]
  to   = google_pubsub_topic.west
}

moved {
  from = google_pubsub_subscription.west[0]
  to   = google_pubsub_subscription.west
}

moved {
  from = google_pubsub_topic_iam_member.west_receiver[0]
  to   = google_pubsub_topic_iam_member.west_receiver
}

moved {
  from = google_pubsub_subscription_iam_member.west_hourly[0]
  to   = google_pubsub_subscription_iam_member.west_hourly
}

moved {
  from = google_monitoring_alert_policy.west_job_failed[0]
  to   = google_monitoring_alert_policy.west_job_failed
}

moved {
  from = google_monitoring_alert_policy.west_pubsub_backlog[0]
  to   = google_monitoring_alert_policy.west_pubsub_backlog
}

moved {
  from = google_monitoring_alert_policy.west_receiver_failure[0]
  to   = google_monitoring_alert_policy.west_receiver_failure
}

moved {
  from = google_storage_bucket.raw_west[0]
  to   = google_storage_bucket.raw_west
}

moved {
  from = google_storage_bucket.preflight_west[0]
  to   = google_storage_bucket.preflight_west
}

moved {
  from = google_service_account.west[0]
  to   = google_service_account.west
}

moved {
  from = google_service_account_iam_member.west_deployer[0]
  to   = google_service_account_iam_member.west_deployer
}

moved {
  from = google_service_account.west_receiver[0]
  to   = google_service_account.west_receiver
}

moved {
  from = google_service_account_iam_member.west_receiver_deployer[0]
  to   = google_service_account_iam_member.west_receiver_deployer
}

moved {
  from = google_secret_manager_secret_iam_member.west_receiver[0]
  to   = google_secret_manager_secret_iam_member.west_receiver
}

moved {
  from = google_cloud_run_v2_service.west[0]
  to   = google_cloud_run_v2_service.west
}

moved {
  from = google_cloud_run_v2_service_iam_member.west_public[0]
  to   = google_cloud_run_v2_service_iam_member.west_public
}

moved {
  from = google_service_account.west_scheduler[0]
  to   = google_service_account.west_scheduler
}

moved {
  from = google_service_account_iam_member.west_scheduler_deployer[0]
  to   = google_service_account_iam_member.west_scheduler_deployer
}

moved {
  from = google_storage_bucket_iam_policy.raw_west[0]
  to   = google_storage_bucket_iam_policy.raw_west
}

moved {
  from = google_storage_bucket_iam_policy.preflight_west[0]
  to   = google_storage_bucket_iam_policy.preflight_west
}

moved {
  from = google_logging_project_bucket_config.west[0]
  to   = google_logging_project_bucket_config.west
}

moved {
  from = google_logging_project_sink.default_west[0]
  to   = google_logging_project_sink.default_west
}
