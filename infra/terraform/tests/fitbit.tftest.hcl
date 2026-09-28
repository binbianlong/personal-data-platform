mock_provider "google" {
  override_during = plan
  mock_data "google_project" {
    defaults = { number = "123456789012" }
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
    condition     = length(google_cloud_run_v2_service.fitbit) == 0 && length(google_cloud_tasks_queue.fitbit) == 0 && length(google_service_account.fitbit) == 0
    error_message = "Fitbit resources must remain disabled by default."
  }
  assert {
    condition     = !contains(local.runtime_services, "cloudtasks.googleapis.com") && !contains(keys(local.runtime_jobs.reconciliation.environment), "PDP_FITBIT_REPAIR_ENABLED") && !contains(keys(local.runtime_jobs.reconciliation.secrets), "PDP_FITBIT_OAUTH_REFRESH_TOKEN")
    error_message = "Disabled Fitbit must not change existing runtime behavior."
  }
}

run "enabled_runtime_contract" {
  command = plan
  variables {
    enable_fitbit_runtime = true
    fitbit_subject_key    = "synthetic-owner"
    fitbit_secret_ids = {
      PDP_FITBIT_OAUTH_CLIENT_ID       = "existing-client-id"
      PDP_FITBIT_OAUTH_CLIENT_SECRET   = "existing-client-secret"
      PDP_FITBIT_OAUTH_REFRESH_TOKEN   = "existing-refresh-token"
      PDP_FITBIT_HEALTH_USER_ID        = "existing-owner"
      PDP_FITBIT_WEBHOOK_AUTHORIZATION = "existing-webhook-secret"
    }
  }
  assert {
    condition     = google_cloud_tasks_queue.fitbit[0].rate_limits[0].max_concurrent_dispatches == 1 && google_cloud_run_v2_service.fitbit[0].template[0].scaling[0].min_instance_count == 0 && google_cloud_run_v2_service.fitbit[0].template[0].max_instance_request_concurrency > 1
    error_message = "One task writer must coexist with concurrent webhook reception and scale to zero."
  }
  assert {
    condition     = length(local.scheduled_jobs) == 2 && local.runtime_jobs.reconciliation.environment.PDP_FITBIT_REPAIR_ENABLED == "true" && local.runtime_jobs.reconciliation.environment.PDP_FITBIT_PROCESSING_PAUSED == "true"
    error_message = "Fitbit repair must reuse the existing schedule and begin paused."
  }
  assert {
    condition     = length(local.runtime_secrets) == 3 && length(google_secret_manager_secret_iam_member.fitbit) == 6 && length(google_secret_manager_secret_iam_member.fitbit_reconciliation) == 4 && local.runtime_jobs.reconciliation.secrets.PDP_FITBIT_OAUTH_REFRESH_TOKEN == "existing-refresh-token" && google_cloud_run_v2_service.fitbit[0].template[0].containers[0].args == tolist(["fitbit", "serve"])
    error_message = "Use existing secret references without provisioning secret versions or legacy resources."
  }
  assert {
    condition     = length([for rule in google_storage_bucket.raw.lifecycle_rule : rule if one(rule.condition).age == 90]) == 3 && length([for rule in google_storage_bucket.raw.lifecycle_rule : rule if contains(one(rule.condition).matches_prefix, "raw/fitbit/v1/") && contains(one(rule.condition).matches_suffix, ".json.gz")]) == 1 && length([for rule in google_storage_bucket.raw.lifecycle_rule : rule if contains(one(rule.condition).matches_prefix, "receipts/fitbit/v1/") && one(rule.condition).age == 90]) == 1
    error_message = "Fitbit Raw and receipts must expire at 90 days, while control JSON is excluded from the Raw suffix rule."
  }
}

run "external_secret_id_collision" {
  command = plan
  variables {
    enable_fitbit_runtime = true
    fitbit_subject_key    = "synthetic-owner"
    fitbit_secret_ids = {
      PDP_FITBIT_OAUTH_CLIENT_ID       = "motherduck_token"
      PDP_FITBIT_OAUTH_CLIENT_SECRET   = "existing-client-secret"
      PDP_FITBIT_OAUTH_REFRESH_TOKEN   = "existing-refresh-token"
      PDP_FITBIT_HEALTH_USER_ID        = "existing-owner"
      PDP_FITBIT_WEBHOOK_AUTHORIZATION = "existing-webhook-secret"
    }
  }
  assert {
    condition     = one([for env in google_cloud_run_v2_job.runtime["reconciliation"].template[0].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].secret if env.name == "PDP_FITBIT_OAUTH_CLIENT_ID"]) == "motherduck_token" && google_secret_manager_secret_iam_member.runtime["reconciliation:motherduck_token"].secret_id == "motherduck-token"
    error_message = "Existing Fitbit secret IDs must never be resolved as Terraform-managed runtime secret keys."
  }
}
