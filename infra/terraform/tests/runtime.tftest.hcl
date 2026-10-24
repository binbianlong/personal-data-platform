mock_provider "google" {
  override_during = plan
  mock_data "google_iam_policy" {
    defaults = { policy_data = "{\"version\":3,\"bindings\":[]}" }
  }
}
variables {
  project_id                     = "example-project"
  deployer_service_account_email = "github-tf-deploy@example-project.iam.gserviceaccount.com"
  rebuild_impersonator_member    = "user:operator@example.com"
  runtime_image_uri              = "us-west1-docker.pkg.dev/example-project/personal-data-platform/runtime@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  alert_email                    = "operator@example.com"
  fitbit_subject_key             = "owner"
  secret_versions = {
    motherduck_token           = "7"
    motherduck_preflight_token = "3"
    fitbit_oauth_config        = "11"
    fitbit_webhook_config      = "13"
    heartbeat_config           = "17"
    owntracks_config           = "19"
  }
}
run "minimum_normal_runtime" {
  command = plan
  assert {
    condition     = google_cloud_run_v2_job.runtime["daily"].template[0].template[0].containers[0].args == tolist(["reconciliation"]) && local.runtime_common_environment.PDP_FITBIT_PROCESSING_PAUSED == "false"
    error_message = "The daily job must run every stage without ignored scope options."
  }
  assert {
    condition     = toset(keys(output.runtime_jobs)) == toset(["daily"]) && toset(keys(output.scheduler_jobs)) == toset(["daily", "fitbit_batch"])
    error_message = "Normal deployment must retain the daily Job and expose its schedule plus the Fitbit batch schedule."
  }
  assert {
    condition     = alltrue([for job in google_cloud_scheduler_job.runtime : !job.paused]) && !google_cloud_scheduler_job.fitbit_batch.paused && google_monitoring_alert_policy.job_failed.enabled && google_monitoring_alert_policy.pubsub_backlog.enabled
    error_message = "Normal operation must keep hourly batches, the daily schedule, and monitoring enabled."
  }
  assert {
    condition     = output.storage_buckets.raw == google_storage_bucket.raw.name && output.storage_buckets.preflight == google_storage_bucket.preflight.name
    error_message = "Storage outputs must point to the active runtime buckets."
  }
}
run "runtime_requires_secret_versions" {
  command = plan
  variables { secret_versions = {} }
  expect_failures = [var.secret_versions]
}
run "runtime_requires_every_secret_version" {
  command = plan
  variables { secret_versions = { motherduck_token = "7" } }
  expect_failures = [var.secret_versions]
}
run "reject_unknown_runtime_secret_key" {
  command = plan
  variables {
    secret_versions = { unknown_secret = "1" }
  }
  expect_failures = [var.secret_versions]
}
run "reject_latest_runtime_secret_version" {
  command = plan
  variables {
    secret_versions = { motherduck_token = "latest" }
  }
  expect_failures = [var.secret_versions]
}
run "reject_zero_runtime_secret_version" {
  command = plan
  variables {
    secret_versions = { motherduck_token = "0" }
  }
  expect_failures = [var.secret_versions]
}
run "reject_negative_runtime_secret_version" {
  command = plan
  variables {
    secret_versions = { motherduck_token = "-1" }
  }
  expect_failures = [var.secret_versions]
}
run "reject_fractional_runtime_secret_version" {
  command = plan
  variables {
    secret_versions = { motherduck_token = "1.5" }
  }
  expect_failures = [var.secret_versions]
}
run "reject_empty_runtime_secret_version" {
  command = plan
  variables {
    secret_versions = { motherduck_token = "" }
  }
  expect_failures = [var.secret_versions]
}
run "reject_leading_zero_runtime_secret_version" {
  command = plan
  variables {
    secret_versions = { motherduck_token = "01" }
  }
  expect_failures = [var.secret_versions]
}
run "reject_newline_runtime_secret_version" {
  command = plan
  variables {
    secret_versions = { motherduck_token = "1\n" }
  }
  expect_failures = [var.secret_versions]
}
run "reject_null_runtime_secret_version" {
  command = plan
  variables {
    secret_versions = { motherduck_token = null }
  }
  expect_failures = [var.secret_versions]
}
run "runtime_storage_and_execution_contract" {
  command = plan
  assert {
    condition     = google_storage_bucket.raw.location == "us-west1" && google_storage_bucket.preflight.location == "us-west1" && google_storage_bucket.preflight.name != google_storage_bucket.raw.name
    error_message = "Active Raw and preflight buckets must be separate and remain in us-west1."
  }
  assert {
    condition     = length(google_cloud_run_v2_job.runtime) == 1 && length(output.scheduler_jobs) == 2 && length(google_cloud_scheduler_job.runtime) == 1 && alltrue([for scheduler in google_cloud_scheduler_job.runtime : !scheduler.paused && scheduler.region == "us-west1" && scheduler.time_zone == "Asia/Tokyo"]) && google_cloud_scheduler_job.fitbit_batch.region == "us-west1"
    error_message = "Normal operation requires one daily Job and separate daily and Fitbit batch schedules in the configured region."
  }
  assert {
    condition     = google_cloud_run_v2_job.runtime["daily"].template[0].template[0].timeout == "6000s" && alltrue([for job in google_cloud_run_v2_job.runtime : job.location == "us-west1" && job.template[0].task_count == 1 && job.template[0].parallelism == 1 && job.template[0].template[0].containers[0].image == var.runtime_image_uri])
    error_message = "Runtime jobs need bounded serialized execution and a regional immutable image."
  }
  assert {
    condition     = google_pubsub_topic.fitbit.message_storage_policy[0].allowed_persistence_regions == toset(["us-west1"]) && google_pubsub_topic.fitbit.message_storage_policy[0].enforce_in_transit && google_pubsub_subscription.fitbit.message_retention_duration == "604800s" && !google_pubsub_subscription.fitbit.retain_acked_messages && google_pubsub_subscription.fitbit.expiration_policy[0].ttl == ""
    error_message = "Pub/Sub must keep only unacked messages for seven days and enforce regional storage/transit."
  }
  assert {
    condition     = length(google_secret_manager_secret.runtime) == 6 && alltrue([for secret in google_secret_manager_secret.runtime : secret.replication[0].user_managed[0].replicas[0].location == "us-west1"])
    error_message = "Secrets must use one regional replica without secret payloads in state."
  }
  assert {
    condition = alltrue([for job_key, expected in {
      daily = { MOTHERDUCK_TOKEN = "7", PDP_FITBIT_OAUTH_CONFIG = "11", PDP_HEARTBEAT_CONFIG = "17" }
      } : tomap({
        for env in google_cloud_run_v2_job.runtime[job_key].template[0].template[0].containers[0].env : env.name => env.value_source[0].secret_key_ref[0].version if length(env.value_source) > 0
    }) == tomap(expected)])
    error_message = "Each runtime Job must inject the specified numeric version for each secret."
  }
  assert {
    condition     = one([for env in google_cloud_run_v2_service.fitbit_receiver.template[0].containers[0].env : env.value_source[0].secret_key_ref[0].version if env.name == "PDP_FITBIT_WEBHOOK_CONFIG"]) == "13"
    error_message = "The Fitbit receiver must inject the specified numeric webhook secret version."
  }
  assert {
    condition     = google_pubsub_topic_iam_member.fitbit_receiver.role == "roles/pubsub.publisher" && !contains(keys(local.runtime_common_environment), "GCS_BUCKET") && google_secret_manager_secret_iam_member.fitbit_receiver.secret_id == google_secret_manager_secret.runtime["fitbit_webhook_config"].secret_id && !contains(keys(local.runtime_job_secret_access), "fitbit_webhook_config")
    error_message = "Receiver credentials and publisher access must be isolated from acquisition OAuth and subscriber access."
  }
  assert {
    condition     = length(google_storage_bucket.raw.lifecycle_rule) == 0 && google_storage_bucket.raw.soft_delete_policy[0].retention_duration_seconds == 0 && google_storage_bucket.preflight.soft_delete_policy[0].retention_duration_seconds == 0
    error_message = "Retained buckets must not retain a Fitbit Raw lifecycle rule or soft-delete storage."
  }
}
run "reject_other_region_image" {
  command = plan
  variables { runtime_image_uri = "us-central1-docker.pkg.dev/example-project/personal-data-platform/runtime@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  expect_failures = [var.runtime_image_uri]
}

run "runtime_storage_and_monitoring_are_separate" {
  command = plan
  override_resource {
    target          = google_service_account.owntracks_receiver
    override_during = plan
    values          = { email = "owntracks@example-project.iam.gserviceaccount.com", name = "projects/example-project/serviceAccounts/owntracks@example-project.iam.gserviceaccount.com" }
  }
  override_resource {
    target          = google_service_account.rebuild_operator
    override_during = plan
    values = {
      email = "rebuild@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/rebuild@example-project.iam.gserviceaccount.com"
    }
  }
  override_resource {
    target          = google_service_account.processing
    override_during = plan
    values = {
      email = "runtime@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/runtime@example-project.iam.gserviceaccount.com"
    }
  }
  override_resource {
    target          = google_service_account.fitbit_receiver
    override_during = plan
    values = {
      email = "receiver@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/receiver@example-project.iam.gserviceaccount.com"
    }
  }
  override_resource {
    target          = google_logging_project_bucket_config.runtime
    override_during = plan
    values          = { id = "projects/example-project/locations/us-west1/buckets/pdp-west" }
  }
  variables { schedulers_enabled = false }
  assert {
    condition     = length(google_pubsub_subscription.fitbit.push_config) == 0 && alltrue([for scheduler in google_cloud_scheduler_job.runtime : scheduler.paused]) && google_cloud_scheduler_job.fitbit_batch.paused && local.runtime_common_environment.PDP_FITBIT_PROCESSING_PAUSED == "true" && !google_monitoring_alert_policy.receiver_failure.enabled
    error_message = "Pausing schedules must pause Fitbit processing and all native alerts."
  }
  assert {
    condition     = !anytrue([for binding in data.google_iam_policy.raw.binding : contains(binding.members, "serviceAccount:${google_service_account.fitbit_receiver.email}")]) && !anytrue([for binding in data.google_iam_policy.preflight.binding : contains(binding.members, "serviceAccount:${google_service_account.processing.email}")])
    error_message = "Receiver must not access Raw; acquisition must not access preflight storage."
  }
  assert {
    condition     = google_logging_project_sink.default.name == "_Default" && strcontains(google_logging_project_sink.default.destination, google_logging_project_bucket_config.runtime.id) && google_logging_project_bucket_config.runtime.location == "us-west1" && !google_monitoring_alert_policy.pubsub_backlog.enabled && !google_monitoring_alert_policy.job_failed.enabled
    error_message = "Logs must retain one regional route and paused processing must suspend native alerts."
  }
  assert {
    condition     = google_logging_project_sink.default.unique_writer_identity
    error_message = "The existing _Default sink reports a Google-managed writer identity; preserve it to avoid persistent deployment drift."
  }
}

run "runtime_pins_and_shared_identity" {
  command = plan
  override_resource {
    target          = google_service_account.processing
    override_during = plan
    values          = { email = "runtime@example-project.iam.gserviceaccount.com", name = "projects/example-project/serviceAccounts/runtime@example-project.iam.gserviceaccount.com" }
  }
  variables {
    secret_versions = { motherduck_token = "7", fitbit_oauth_config = "11", fitbit_webhook_config = "13", heartbeat_config = "17", owntracks_config = "19" }
  }
  assert {
    condition     = google_cloud_run_v2_service.fitbit_worker.template[0].service_account == google_cloud_run_v2_job.runtime["daily"].template[0].template[0].service_account && google_cloud_scheduler_job.runtime["daily"].schedule == "10 4 * * *"
    error_message = "Batch and daily processing with one runtime identity and non-overlapping daily schedule are required."
  }
  assert {
    condition     = google_monitoring_alert_policy.job_failed.enabled && length(google_monitoring_alert_policy.pubsub_backlog.conditions) == 2 && google_monitoring_alert_policy.pubsub_backlog.conditions[0].condition_threshold[0].threshold_value == 4500
    error_message = "Hourly Fitbit batches must retain their 75-minute backlog threshold."
  }
  assert {
    condition     = google_monitoring_alert_policy.pubsub_backlog.combiner == "OR" && google_monitoring_alert_policy.pubsub_backlog.conditions[1].condition_threshold[0].threshold_value == 900 && alltrue([for condition in google_monitoring_alert_policy.pubsub_backlog.conditions : condition.condition_threshold[0].duration == "300s"]) && strcontains(google_monitoring_alert_policy.pubsub_backlog.conditions[0].condition_threshold[0].filter, "subscription_id = \"pdp-fitbit-west-pull\"") && !strcontains(google_monitoring_alert_policy.pubsub_backlog.conditions[0].condition_threshold[0].filter, "pdp-owntracks-west") && strcontains(google_monitoring_alert_policy.pubsub_backlog.conditions[1].condition_threshold[0].filter, "subscription_id = \"pdp-owntracks-west-push\"") && !strcontains(google_monitoring_alert_policy.pubsub_backlog.conditions[1].condition_threshold[0].filter, "pdp-fitbit-west")
    error_message = "Backlog monitoring must isolate each queue and retain OwnTracks' 15-minute threshold for five minutes."
  }
}
