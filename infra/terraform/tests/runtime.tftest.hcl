mock_provider "google" {
  override_during = plan
  mock_data "google_iam_policy" { defaults = { policy_data = "{\"version\":3,\"bindings\":[]}" } }
}
variables {
  project_id                     = "example-project"
  deployer_service_account_email = "github-tf-deploy@example-project.iam.gserviceaccount.com"
  collector_impersonator_member  = "user:operator@example.com"
  alert_email                    = "operator@example.com"
  fitbit_subject_key             = "owner"
  west_image_uri                 = "us-west1-docker.pkg.dev/example-project/personal-data-platform/runtime@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  west_secret_versions           = { motherduck_token = "7", fitbit_oauth_config = "11", fitbit_webhook_config = "13", heartbeat_config = "17" }
}
run "runtime_keeps_backups" {
  command = plan
  assert {
    condition     = google_storage_bucket.raw.location == "us-central1" && !google_storage_bucket.raw.force_destroy && google_storage_bucket.raw.soft_delete_policy[0].retention_duration_seconds == 0 && length(google_storage_bucket.raw.lifecycle_rule) == 0
    error_message = "Retained legacy Screen Time Raw must stay in place with bounded retention."
  }
}
run "backup_iam_is_read_only" {
  command = plan
  override_resource {
    target          = google_service_account.rebuild_operator
    override_during = plan
    values          = { email = "rebuild@example-project.iam.gserviceaccount.com", name = "projects/example-project/serviceAccounts/rebuild@example-project.iam.gserviceaccount.com" }
  }
  assert {
    condition     = length(data.google_iam_policy.raw_bucket.binding) == 2 && one([for binding in data.google_iam_policy.raw_bucket.binding : binding.members if binding.role == "roles/storage.objectViewer"]) == toset(["serviceAccount:rebuild@example-project.iam.gserviceaccount.com"])
    error_message = "Old Raw may be read for explicit recovery; no collector or processing writer remains."
  }
  assert {
    condition     = length(data.google_iam_policy.preflight_bucket.binding) == 1 && one(data.google_iam_policy.preflight_bucket.binding).role == "roles/storage.admin"
    error_message = "Old preflight must grant access only to its owner."
  }
}
