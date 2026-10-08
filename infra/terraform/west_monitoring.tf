resource "google_monitoring_alert_policy" "west_job_failed" {
  project      = var.project_id
  display_name = "PDP west processing Job failed"
  combiner     = "OR"
  enabled      = var.west_schedulers_enabled
  conditions {
    display_name = "Failed daily execution"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND resource.labels.location = \"${var.west_region}\" AND resource.labels.project_id = \"${var.project_id}\" AND resource.labels.job_name = \"${local.west_jobs.daily.name}\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result != \"succeeded\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_DELTA"
      }
      trigger { count = 1 }
    }
  }
  alert_strategy { auto_close = "1800s" }
  notification_channels = [google_monitoring_notification_channel.email.name]
  depends_on            = [google_project_service.runtime]
}
resource "google_monitoring_alert_policy" "west_pubsub_backlog" {
  project      = var.project_id
  enabled      = var.west_schedulers_enabled
  display_name = "Fitbit west: oldest unacked notification over 15 minutes"
  combiner     = "OR"
  conditions {
    display_name = "Unacked message age exceeds 15 minutes"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND resource.labels.subscription_id = \"${google_pubsub_subscription.west.name}\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
      comparison      = "COMPARISON_GT"
      threshold_value = 900
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.name]
}
resource "google_monitoring_alert_policy" "west_receiver_failure" {
  project      = var.project_id
  enabled      = var.west_schedulers_enabled
  display_name = "Fitbit west receiver or worker error"
  combiner     = "OR"
  conditions {
    display_name = "Receiver or worker ERROR log"
    condition_matched_log {
      filter = "resource.type=\"cloud_run_revision\" AND resource.labels.location=\"${var.west_region}\" AND resource.labels.service_name=(\"pdp-fitbit-west\" OR \"pdp-fitbit-worker-west\") AND severity>=ERROR"
    }
  }
  alert_strategy {
    notification_rate_limit { period = "3600s" }
    auto_close = "86400s"
  }
  notification_channels = [google_monitoring_notification_channel.email.name]
}
