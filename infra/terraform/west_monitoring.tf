resource "google_monitoring_alert_policy" "west_job_failed" {
  count        = var.enable_west_runtime ? 1 : 0
  project      = var.project_id
  display_name = "PDP west processing Job failed"
  combiner     = "OR"
  enabled      = var.west_schedulers_enabled
  conditions {
    display_name = "Failed hourly or daily execution"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND resource.labels.location = \"${var.west_region}\" AND resource.labels.project_id = \"${var.project_id}\" AND (resource.labels.job_name = \"${local.west_jobs.hourly.name}\" OR resource.labels.job_name = \"${local.west_jobs.daily.name}\") AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result != \"succeeded\""
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
  count        = var.enable_west_runtime ? 1 : 0
  project      = var.project_id
  enabled      = var.west_schedulers_enabled
  display_name = "Fitbit west: oldest unacked notification over 24 hours"
  combiner     = "OR"
  conditions {
    display_name = "Unacked message age exceeds 24 hours"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND resource.labels.subscription_id = \"${google_pubsub_subscription.west[0].name}\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
      comparison      = "COMPARISON_GT"
      threshold_value = 86400
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.name]
}
resource "google_monitoring_alert_policy" "west_receiver_failure" {
  count        = var.enable_west_runtime ? 1 : 0
  project      = var.project_id
  enabled      = var.west_schedulers_enabled
  display_name = "Fitbit west receiver error"
  combiner     = "OR"
  conditions {
    display_name = "Receiver ERROR log"
    condition_matched_log {
      filter = "resource.type=\"cloud_run_revision\" AND resource.labels.location=\"${var.west_region}\" AND resource.labels.service_name=\"pdp-fitbit-west\" AND severity>=ERROR"
    }
  }
  alert_strategy {
    notification_rate_limit { period = "3600s" }
    auto_close = "86400s"
  }
  notification_channels = [google_monitoring_notification_channel.email.name]
}
