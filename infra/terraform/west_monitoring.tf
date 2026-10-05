resource "google_logging_metric" "west_job_error" {
  for_each = local.west_jobs

  project     = var.project_id
  name        = "pdp-${each.value.name}-error"
  description = "ERROR-or-higher log entries emitted by ${each.value.name}"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.location=\"${var.west_region}\"",
    "resource.labels.project_id=\"${var.project_id}\"",
    "resource.labels.job_name=\"${each.value.name}\"",
    "severity>=ERROR",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }

  depends_on = [google_project_service.runtime]
}

resource "google_monitoring_alert_policy" "west_job_failed" {
  for_each = local.west_jobs

  project      = var.project_id
  display_name = "${each.value.name}: Cloud Run Job failed"
  combiner     = "OR"
  enabled      = var.west_schedulers_enabled

  conditions {
    display_name = "Failed ${each.value.name} execution"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "resource.labels.location = \"${var.west_region}\"",
        "resource.labels.project_id = \"${var.project_id}\"",
        "resource.labels.job_name = \"${each.value.name}\"",
        "metric.type = \"run.googleapis.com/job/completed_execution_count\"",
        "metric.labels.result != \"succeeded\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_DELTA"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    mime_type = "text/markdown"
    content   = "The `${each.value.name}` Cloud Run Job completed unsuccessfully. Inspect the execution and structured logs before retrying."
  }

  user_labels = {
    application = "personal-data-platform"
    role        = each.key
  }

  depends_on = [google_project_service.runtime]
}

resource "google_monitoring_alert_policy" "west_job_error_log" {
  for_each = local.west_jobs

  project      = var.project_id
  display_name = "${each.value.name}: application error log"
  combiner     = "OR"
  enabled      = var.west_schedulers_enabled

  conditions {
    display_name = "ERROR log from ${each.value.name}"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "resource.labels.location = \"${var.west_region}\"",
        "resource.labels.project_id = \"${var.project_id}\"",
        "resource.labels.job_name = \"${each.value.name}\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.west_job_error[each.key].name}\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_DELTA"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    mime_type = "text/markdown"
    content   = "The `${each.value.name}` Cloud Run Job emitted an ERROR-or-higher log entry. Inspect the structured error context."
  }

  user_labels = {
    application = "personal-data-platform"
    role        = each.key
  }

  depends_on = [google_project_service.runtime]
}

resource "google_monitoring_alert_policy" "west_pubsub_backlog" {
  count        = var.enable_west_runtime ? 1 : 0
  project      = var.project_id
  enabled      = var.west_schedulers_enabled
  display_name = "Fitbit west: oldest unacked notification over 12 hours"
  combiner     = "OR"
  conditions {
    display_name = "Unacked message age exceeds 12 hours"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND resource.labels.subscription_id = \"${google_pubsub_subscription.west[0].name}\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
      comparison      = "COMPARISON_GT"
      threshold_value = 43200
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.name]
}
resource "google_logging_metric" "west_daily_success" {
  count   = var.enable_west_runtime ? 1 : 0
  project = var.project_id
  name    = "pdp-reconciliation-west-success"
  filter  = "resource.type=\"cloud_run_job\" AND resource.labels.location=\"${var.west_region}\" AND resource.labels.job_name=\"reconciliation-west\" AND jsonPayload.event=\"reconciliation\" AND jsonPayload.status=\"succeeded\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [google_project_service.runtime]
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
