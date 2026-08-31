resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "personal-data-platform operations email"
  type         = "email"

  labels = {
    email_address = var.alert_email
  }

  user_labels = {
    application = "personal-data-platform"
    managed_by  = "terraform"
  }

  depends_on = [google_project_service.runtime]
}

resource "google_logging_metric" "job_error" {
  for_each = local.runtime_jobs

  project     = var.project_id
  name        = "pdp-${each.value.name}-error"
  description = "ERROR-or-higher log entries emitted by ${each.value.name}"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
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

resource "google_monitoring_alert_policy" "job_failed" {
  for_each = local.runtime_jobs

  project      = var.project_id
  display_name = "${each.value.name}: Cloud Run Job failed"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "Failed ${each.value.name} execution"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
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

resource "google_monitoring_alert_policy" "job_error_log" {
  for_each = local.runtime_jobs

  project      = var.project_id
  display_name = "${each.value.name}: application error log"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "ERROR log from ${each.value.name}"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "resource.labels.job_name = \"${each.value.name}\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.job_error[each.key].name}\"",
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
