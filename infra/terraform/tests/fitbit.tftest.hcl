mock_provider "google" {
  override_during = plan
  mock_resource "google_monitoring_notification_channel" {
    defaults = { name = "projects/example-project/notificationChannels/123" }
  }
}

variables {
  project_id                     = "example-project"
  deployer_service_account_email = "github-tf-deploy@example-project.iam.gserviceaccount.com"
  collector_impersonator_member  = "user:operator@example.com"
  image_uri                      = "us-central1-docker.pkg.dev/example-project/personal-data-platform/app@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  alert_email                    = "operator@example.com"
}

run "disabled_by_default" {
  command = plan
  assert {
    condition     = !contains(local.runtime_services, "cloudtasks.googleapis.com") && !contains(keys(local.runtime_jobs.reconciliation.environment), "PDP_FITBIT_DAILY_ENABLED") && length(local.fitbit_reconciliation_secrets) == 0
    error_message = "Disabled Fitbit must not change existing runtime behavior."
  }
  assert {
    condition     = length(google_logging_metric.fitbit_daily_success) == 0 && length(google_logging_metric.fitbit_daily_age) == 0 && length(google_monitoring_alert_policy.fitbit_daily_absent) == 0
    error_message = "Disabled Fitbit must not provision daily monitoring."
  }
}

run "enabled_daily_contract" {
  command = plan
  variables {
    enable_fitbit_runtime = true
    fitbit_subject_key    = "synthetic-owner"
    fitbit_secret_ids = {
      PDP_FITBIT_OAUTH_CLIENT_ID     = "existing-client-id"
      PDP_FITBIT_OAUTH_CLIENT_SECRET = "existing-client-secret"
      PDP_FITBIT_OAUTH_REFRESH_TOKEN = "existing-refresh-token"
    }
  }
  assert {
    condition     = length(local.scheduled_jobs) == 1 && local.runtime_jobs.reconciliation.environment.PDP_FITBIT_DAILY_ENABLED == "true" && local.runtime_jobs.reconciliation.environment.PDP_FITBIT_PROCESSING_PAUSED == "true" && !contains(local.runtime_services, "cloudtasks.googleapis.com")
    error_message = "Daily Fitbit must reuse the existing schedule, begin paused and need no task API."
  }
  assert {
    condition     = length(google_secret_manager_secret_iam_member.fitbit_reconciliation) == 3 && local.runtime_jobs.reconciliation.secrets.PDP_FITBIT_OAUTH_REFRESH_TOKEN == "existing-refresh-token" && local.fitbit_storage_members == tolist(["serviceAccount:reconciliation@example-project.iam.gserviceaccount.com"])
    error_message = "Only the shared daily account needs Fitbit credentials and Raw creation."
  }
  assert {
    condition     = length([for rule in google_storage_bucket.raw.lifecycle_rule : rule if contains(one(rule.condition).matches_prefix, "raw/fitbit/v2/") && contains(one(rule.condition).matches_prefix, "raw/fitbit/v1/") && one(rule.condition).age == 90 && contains(one(rule.condition).matches_suffix, ".json.gz")]) == 1 && length([for rule in google_storage_bucket.raw.lifecycle_rule : rule if contains(one(rule.condition).matches_prefix, "receipts/fitbit/v1/") && one(rule.condition).age == 90]) == 1
    error_message = "Legacy and bundled Raw, and remaining old receipts, must expire at 90 days."
  }
  assert {
    condition     = length(google_logging_metric.fitbit_daily_success) == 1 && !google_monitoring_alert_policy.fitbit_daily_absent[0].enabled
    error_message = "Paused Fitbit must disable absence notifications."
  }
  assert {
    condition     = length([for rule in google_storage_bucket.raw.lifecycle_rule : rule if contains(one(rule.condition).matches_prefix, "raw/fitbit/v1/_control/") && one(rule.condition).age == 90 && contains(one(rule.condition).matches_suffix, ".json")]) == 1
    error_message = "Retired mutable Fitbit control objects must expire as well."
  }
}

run "active_daily_monitoring" {
  command = plan
  variables {
    enable_fitbit_runtime    = true
    fitbit_processing_paused = false
    fitbit_subject_key       = "synthetic-owner"
    fitbit_secret_ids = {
      PDP_FITBIT_OAUTH_CREDENTIALS = "existing-oauth-bundle"
    }
  }
  assert {
    condition = alltrue([
      google_monitoring_alert_policy.fitbit_daily_absent[0].enabled,
      google_logging_metric.fitbit_daily_success[0].metric_descriptor[0].metric_kind == "DELTA",
      google_logging_metric.fitbit_daily_success[0].metric_descriptor[0].value_type == "INT64",
      length(google_logging_metric.fitbit_daily_success[0].metric_descriptor[0].labels) == 0,
      google_logging_metric.fitbit_daily_age[0].metric_descriptor[0].value_type == "DISTRIBUTION",
      google_logging_metric.fitbit_daily_age[0].value_extractor == "EXTRACT(jsonPayload.summary.full_success_age_seconds)",
      google_monitoring_alert_policy.fitbit_daily_absent[0].conditions[0].condition_threshold[0].threshold_value == 172800,
      google_monitoring_alert_policy.fitbit_daily_absent[0].notification_channels == tolist([google_monitoring_notification_channel.email.name]),
      strcontains(google_logging_metric.fitbit_daily_success[0].filter, "resource.labels.job_name=\"reconciliation\""),
      strcontains(google_logging_metric.fitbit_daily_success[0].filter, "jsonPayload.event=\"fitbit_daily\""),
      strcontains(google_logging_metric.fitbit_daily_success[0].filter, "jsonPayload.status=\"succeeded\""),
    ])
    error_message = "Only full daily success may reset Fitbit's independent 48-hour monitoring."
  }
}

run "bundled_oauth_credentials" {
  command = plan
  variables {
    enable_fitbit_runtime = true
    fitbit_subject_key    = "synthetic-owner"
    fitbit_secret_ids = {
      PDP_FITBIT_OAUTH_CREDENTIALS = "existing-oauth-bundle"
    }
  }
  assert {
    condition = alltrue([
      length(google_secret_manager_secret_iam_member.fitbit_reconciliation) == 1,
      local.runtime_jobs.reconciliation.secrets.PDP_FITBIT_OAUTH_CREDENTIALS == "existing-oauth-bundle",
      !contains(keys(local.runtime_jobs.reconciliation.secrets), "PDP_FITBIT_WEBHOOK_AUTHORIZATION"),
      !contains(keys(local.runtime_jobs.reconciliation.secrets), "PDP_FITBIT_HEALTH_USER_ID"),
      one([for env in google_cloud_run_v2_job.runtime["reconciliation"].template[0].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].secret if env.name == "PDP_FITBIT_OAUTH_CREDENTIALS"]) == "existing-oauth-bundle",
    ])
    error_message = "Daily collection needs one OAuth bundle without webhook credentials."
  }
}

run "mixed_oauth_references_rejected" {
  command = plan
  variables {
    enable_fitbit_runtime = true
    fitbit_subject_key    = "synthetic-owner"
    fitbit_secret_ids = {
      PDP_FITBIT_OAUTH_CREDENTIALS = "existing-oauth-bundle"
      PDP_FITBIT_OAUTH_CLIENT_ID   = "existing-client-id"
    }
  }
  expect_failures = [var.fitbit_secret_ids]
}

run "external_secret_id_collision" {
  command = plan
  variables {
    enable_fitbit_runtime = true
    fitbit_subject_key    = "synthetic-owner"
    fitbit_secret_ids = {
      PDP_FITBIT_OAUTH_CLIENT_ID     = "motherduck_token"
      PDP_FITBIT_OAUTH_CLIENT_SECRET = "existing-client-secret"
      PDP_FITBIT_OAUTH_REFRESH_TOKEN = "existing-refresh-token"
    }
  }
  assert {
    condition     = one([for env in google_cloud_run_v2_job.runtime["reconciliation"].template[0].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].secret if env.name == "PDP_FITBIT_OAUTH_CLIENT_ID"]) == "motherduck_token" && google_secret_manager_secret_iam_member.runtime["reconciliation:motherduck_token"].secret_id == "motherduck-token"
    error_message = "Existing Fitbit secret IDs must remain external references."
  }
}
