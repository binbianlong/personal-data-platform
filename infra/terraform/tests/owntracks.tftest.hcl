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
  west_secret_versions           = { motherduck_token = "7", fitbit_oauth_config = "11", fitbit_webhook_config = "13", heartbeat_config = "17", owntracks_config = "19" }
}
run "owntracks_durable_immediate_ingestion" {
  command = plan
  override_resource {
    target          = google_service_account.west_scheduler
    override_during = plan
    values          = { email = "scheduler@example-project.iam.gserviceaccount.com", name = "projects/example-project/serviceAccounts/scheduler@example-project.iam.gserviceaccount.com" }
  }
  override_resource {
    target          = google_pubsub_topic.owntracks
    override_during = plan
    values          = { id = "projects/example-project/topics/pdp-owntracks-west" }
  }
  assert {
    condition     = google_storage_notification.owntracks.event_types == toset(["OBJECT_FINALIZE"]) && google_storage_notification.owntracks.object_name_prefix == "raw/owntracks/v1/" && google_storage_notification.owntracks.payload_format == "JSON_API_V1" && google_storage_notification.owntracks.topic == google_pubsub_topic.owntracks.id
    error_message = "Only immutable OwnTracks Raw creation must trigger a generation-bearing notification."
  }
  assert {
    condition     = google_cloud_run_v2_service.owntracks["receiver"].template[0].scaling[0].min_instance_count == 0 && google_cloud_run_v2_service.owntracks["worker"].template[0].scaling[0].max_instance_count == 1 && google_cloud_run_v2_service.owntracks["worker"].template[0].max_instance_request_concurrency == 1
    error_message = "OwnTracks must scale to zero and serialize the warehouse worker."
  }
  assert {
    condition     = google_cloud_run_v2_service_iam_member.owntracks_worker.member == "serviceAccount:${google_service_account.west_scheduler.email}" && google_cloud_run_v2_service_iam_member.owntracks_public.member == "allUsers" && length(google_pubsub_subscription.owntracks.push_config) == 1
    error_message = "Only receiver is public; the worker requires authenticated push."
  }
  assert {
    condition     = one([for env in google_cloud_run_v2_service.owntracks["receiver"].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].version if env.name == "PDP_OWNTRACKS_CONFIG"]) == "19" && google_secret_manager_secret_iam_member.owntracks_receiver.secret_id == google_secret_manager_secret.west["owntracks_config"].secret_id
    error_message = "Only receiver must receive the numerically pinned OwnTracks authentication secret."
  }
  assert {
    condition     = length(google_storage_bucket.raw_west.lifecycle_rule) == 0 && contains([for env in google_cloud_run_v2_job.west["daily"].template[0].template[0].containers[0].env : env.name], "GCS_BUCKET") && google_pubsub_subscription.owntracks.message_retention_duration == "604800s"
    error_message = "Raw must be permanent and daily processing able to repair expired notification gaps."
  }
}
run "owntracks_pause_keeps_raw_and_queue" {
  command = plan
  variables { west_schedulers_enabled = false }
  assert {
    condition     = length(google_pubsub_subscription.owntracks.push_config) == 0 && google_storage_notification.owntracks.event_types == toset(["OBJECT_FINALIZE"])
    error_message = "Pause must keep Raw storage notifications and stop worker delivery."
  }
}
