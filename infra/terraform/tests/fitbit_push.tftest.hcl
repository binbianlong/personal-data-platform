mock_provider "google" {
  override_during = plan
  mock_data "google_iam_policy" {
    defaults = { policy_data = "{\"version\":3,\"bindings\":[]}" }
  }
}

variables {
  project_id                     = "example-project"
  deployer_service_account_email = "github-tf-deploy@example-project.iam.gserviceaccount.com"
  collector_impersonator_member  = "user:operator@example.com"
  west_image_uri                 = "us-west1-docker.pkg.dev/example-project/personal-data-platform/runtime@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  alert_email                    = "operator@example.com"
  fitbit_subject_key             = "owner"
  west_secret_versions           = { motherduck_token = "7", fitbit_oauth_config = "11", fitbit_webhook_config = "13", heartbeat_config = "17" }
}

run "push_processing_contract" {
  command = plan
  override_resource {
    target          = google_cloud_run_v2_service.fitbit_worker
    override_during = plan
    values          = { uri = "https://worker.example.run.app" }
  }
  override_resource {
    target          = google_service_account.west_scheduler
    override_during = plan
    values          = { email = "scheduler@example-project.iam.gserviceaccount.com", name = "projects/example-project/serviceAccounts/scheduler@example-project.iam.gserviceaccount.com" }
  }
  assert {
    condition     = toset(keys(output.runtime_jobs)) == toset(["daily"]) && toset(keys(output.scheduler_jobs)) == toset(["daily"])
    error_message = "Notification ingestion must not retain a polling Job or Scheduler."
  }
  assert {
    condition     = google_cloud_run_v2_service.fitbit_worker.template[0].max_instance_request_concurrency == 1 && google_cloud_run_v2_service.fitbit_worker.template[0].scaling[0].min_instance_count == 0 && google_cloud_run_v2_service.fitbit_worker.template[0].scaling[0].max_instance_count == 1 && google_cloud_run_v2_service.fitbit_worker.template[0].timeout == "540s"
    error_message = "Push processing must be serialized, scale to zero, and finish before acknowledgement expiry."
  }
  assert {
    condition     = google_pubsub_subscription.west.ack_deadline_seconds == 600 && google_pubsub_subscription.west.push_config[0].push_endpoint == "${google_cloud_run_v2_service.fitbit_worker.uri}/notifications/fitbit" && google_pubsub_subscription.west.push_config[0].oidc_token[0].audience == google_cloud_run_v2_service.fitbit_worker.uri && google_pubsub_subscription.west.push_config[0].oidc_token[0].service_account_email == google_service_account.west_scheduler.email
    error_message = "Existing notifications must be delivered to the IAM-authenticated worker."
  }
  assert {
    condition     = google_cloud_run_v2_service_iam_member.fitbit_worker.member == "serviceAccount:${google_service_account.west_scheduler.email}" && google_cloud_run_v2_service_iam_member.fitbit_worker.role == "roles/run.invoker" && !contains(keys(local.west_common_environment), "GCS_BUCKET")
    error_message = "Only the trigger identity may invoke the worker and ingestion must not require GCS."
  }
}
