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
  enable_west_runtime            = true
  west_secret_versions = {
    motherduck_token           = "7"
    motherduck_preflight_token = "3"
    fitbit_oauth_config        = "11"
    fitbit_webhook_config      = "13"
    heartbeat_config           = "17"
  }
}
run "minimum_normal_runtime" {
  command = plan
  variables { west_schedulers_enabled = true }
  assert {
    condition     = toset(keys(output.runtime_jobs)) == toset(["hourly", "daily"]) && toset(keys(output.scheduler_jobs)) == toset(["hourly", "daily"])
    error_message = "Normal deployment must expose only hourly Fitbit and the combined daily job, with two schedules."
  }
  assert {
    condition     = alltrue([for job in google_cloud_scheduler_job.west : !job.paused]) && alltrue([for policy in google_monitoring_alert_policy.west_job_failed : policy.enabled]) && google_monitoring_alert_policy.west_pubsub_backlog[0].enabled
    error_message = "Cutover must enable both schedules and native monitoring together."
  }
}
run "west_disabled_without_secret_versions" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = {}
  }
  assert {
    condition     = length(google_cloud_run_v2_job.west) == 0 && length(google_cloud_run_v2_service.west) == 0 && length(google_secret_manager_secret.west) == 0 && length(google_cloud_scheduler_job.west) == 0
    error_message = "Disabled preparation must accept an empty secret version map and create no processing resources."
  }
}
run "west_requires_secret_versions" {
  command = plan
  variables { west_secret_versions = {} }
  expect_failures = [var.west_secret_versions]
}
run "west_requires_every_secret_version" {
  command = plan
  variables { west_secret_versions = { motherduck_token = "7" } }
  expect_failures = [var.west_secret_versions]
}
run "reject_unknown_west_secret_key" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = { unknown_secret = "1" }
  }
  expect_failures = [var.west_secret_versions]
}
run "reject_latest_west_secret_version" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = { motherduck_token = "latest" }
  }
  expect_failures = [var.west_secret_versions]
}
run "reject_zero_west_secret_version" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = { motherduck_token = "0" }
  }
  expect_failures = [var.west_secret_versions]
}
run "reject_negative_west_secret_version" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = { motherduck_token = "-1" }
  }
  expect_failures = [var.west_secret_versions]
}
run "reject_fractional_west_secret_version" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = { motherduck_token = "1.5" }
  }
  expect_failures = [var.west_secret_versions]
}
run "reject_empty_west_secret_version" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = { motherduck_token = "" }
  }
  expect_failures = [var.west_secret_versions]
}
run "reject_leading_zero_west_secret_version" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = { motherduck_token = "01" }
  }
  expect_failures = [var.west_secret_versions]
}
run "reject_newline_west_secret_version" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = { motherduck_token = "1\n" }
  }
  expect_failures = [var.west_secret_versions]
}
run "reject_null_west_secret_version" {
  command = plan
  variables {
    enable_west_runtime  = false
    west_secret_versions = { motherduck_token = null }
  }
  expect_failures = [var.west_secret_versions]
}
run "west_parallel_contract" {
  command = plan
  assert {
    condition     = google_storage_bucket.raw.location == "us-central1" && google_storage_bucket.raw_west[0].location == "us-west1" && google_storage_bucket.preflight_west[0].name != google_storage_bucket.raw_west[0].name && google_storage_bucket.raw.name != google_storage_bucket.raw_west[0].name
    error_message = "Legacy storage must remain in place while west uses distinct buckets."
  }
  assert {
    condition     = length(google_cloud_run_v2_job.west) == 2 && length(google_cloud_scheduler_job.west) == 2 && alltrue([for scheduler in google_cloud_scheduler_job.west : scheduler.paused && scheduler.region == "us-west1" && scheduler.time_zone == "Asia/Tokyo"])
    error_message = "Preparation creates west jobs and exactly two paused schedules."
  }
  assert {
    condition     = google_cloud_run_v2_job.west["hourly"].template[0].template[0].timeout == "3000s" && google_cloud_run_v2_job.west["daily"].template[0].template[0].timeout == "6000s" && alltrue([for job in google_cloud_run_v2_job.west : job.location == "us-west1" && job.template[0].task_count == 1 && job.template[0].parallelism == 1 && job.template[0].template[0].containers[0].image == var.west_image_uri])
    error_message = "West jobs need bounded serialized execution and a regional immutable image."
  }
  assert {
    condition     = google_pubsub_topic.west[0].message_storage_policy[0].allowed_persistence_regions == toset(["us-west1"]) && google_pubsub_topic.west[0].message_storage_policy[0].enforce_in_transit && google_pubsub_subscription.west[0].message_retention_duration == "604800s" && !google_pubsub_subscription.west[0].retain_acked_messages && google_pubsub_subscription.west[0].expiration_policy[0].ttl == ""
    error_message = "Pub/Sub must keep only unacked messages for seven days and enforce west storage/transit."
  }
  assert {
    condition     = length(google_secret_manager_secret.west) == 5 && alltrue([for secret in google_secret_manager_secret.west : secret.replication[0].user_managed[0].replicas[0].location == "us-west1"])
    error_message = "Global west secrets must use one west replica without secret payloads in state."
  }
  assert {
    condition = alltrue([for job_key, expected in {
      hourly = { MOTHERDUCK_TOKEN = "7", PDP_FITBIT_OAUTH_CONFIG = "11" }
      daily  = { MOTHERDUCK_TOKEN = "7", PDP_FITBIT_OAUTH_CONFIG = "11", PDP_HEARTBEAT_CONFIG = "17" }
      } : tomap({
        for env in google_cloud_run_v2_job.west[job_key].template[0].template[0].containers[0].env : env.name => env.value_source[0].secret_key_ref[0].version if length(env.value_source) > 0
    }) == tomap(expected)])
    error_message = "Each west Job must inject the specified numeric version for each secret."
  }
  assert {
    condition     = one([for env in google_cloud_run_v2_service.west[0].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].version if env.name == "PDP_FITBIT_WEBHOOK_CONFIG"]) == "13"
    error_message = "The west receiver must inject the specified numeric webhook secret version."
  }
  assert {
    condition     = google_pubsub_topic_iam_member.west_receiver[0].role == "roles/pubsub.publisher" && google_pubsub_subscription_iam_member.west_hourly[0].role == "roles/pubsub.subscriber" && toset(keys(local.west_jobs.hourly.secrets)) == toset(["MOTHERDUCK_TOKEN", "PDP_FITBIT_OAUTH_CONFIG"]) && length(google_secret_manager_secret_iam_member.west_receiver) == 1 && !contains(keys(local.west_job_secret_access), "fitbit_webhook_config")
    error_message = "Receiver credentials and publisher access must be isolated from acquisition OAuth and subscriber access."
  }
  assert {
    condition     = anytrue([for rule in google_storage_bucket.raw_west[0].lifecycle_rule : one(rule.condition).days_since_custom_time == 90]) && google_storage_bucket.raw_west[0].soft_delete_policy[0].retention_duration_seconds == 0 && google_storage_bucket.preflight_west[0].soft_delete_policy[0].retention_duration_seconds == 0
    error_message = "West copied Raw must retain its original expiration without soft-delete storage."
  }
}
run "reject_legacy_image_in_west" {
  command = plan
  variables { west_image_uri = "us-central1-docker.pkg.dev/example-project/personal-data-platform/runtime@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  expect_failures = [var.west_image_uri]
}

run "west_storage_and_monitoring_are_separate" {
  command = plan
  override_resource {
    target          = google_service_account.collector
    override_during = plan
    values = {
      email = "collector@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/collector@example-project.iam.gserviceaccount.com"
    }
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
    target          = google_service_account.west[0]
    override_during = plan
    values = {
      email = "runtime@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/runtime@example-project.iam.gserviceaccount.com"
    }
  }
  override_resource {
    target          = google_service_account.west_receiver[0]
    override_during = plan
    values = {
      email = "receiver@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/receiver@example-project.iam.gserviceaccount.com"
    }
  }
  override_resource {
    target          = google_logging_project_bucket_config.west[0]
    override_during = plan
    values          = { id = "projects/example-project/locations/us-west1/buckets/pdp-west" }
  }
  variables { west_logging_enabled = true }
  assert {
    condition     = !anytrue([for binding in data.google_iam_policy.raw_west[0].binding : contains(binding.members, "serviceAccount:${google_service_account.west_receiver[0].email}")]) && !anytrue([for binding in data.google_iam_policy.preflight_west[0].binding : contains(binding.members, "serviceAccount:${google_service_account.west[0].email}")])
    error_message = "Receiver must not access Raw; acquisition must not access preflight storage."
  }
  assert {
    condition     = google_logging_project_sink.default_west[0].name == "_Default" && strcontains(google_logging_project_sink.default_west[0].destination, google_logging_project_bucket_config.west[0].id) && google_logging_project_bucket_config.west[0].location == "us-west1" && !google_monitoring_alert_policy.west_pubsub_backlog[0].enabled && alltrue([for policy in google_monitoring_alert_policy.west_job_failed : !policy.enabled])
    error_message = "Ordinary logs must have one west route and paused preparation must not send missing-work alerts."
  }
  assert {
    condition     = google_logging_project_sink.default_west[0].unique_writer_identity
    error_message = "The existing _Default sink reports a Google-managed writer identity; preserve it to avoid persistent deployment drift."
  }
}

run "west_only_runtime_pins_and_shared_identity" {
  command = plan
  override_resource {
    target          = google_service_account.west[0]
    override_during = plan
    values          = { email = "runtime@example-project.iam.gserviceaccount.com", name = "projects/example-project/serviceAccounts/runtime@example-project.iam.gserviceaccount.com" }
  }
  variables {
    west_secret_versions = { motherduck_token = "7", fitbit_oauth_config = "11", fitbit_webhook_config = "13", heartbeat_config = "17" }
  }
  assert {
    condition     = length(google_service_account.west) == 1 && google_cloud_run_v2_job.west["hourly"].template[0].template[0].service_account == google_cloud_run_v2_job.west["daily"].template[0].template[0].service_account && google_cloud_scheduler_job.west["daily"].schedule == "10 4 * * *"
    error_message = "Only two processing jobs with one runtime identity and non-overlapping daily schedule are required."
  }
  assert {
    condition     = length(google_monitoring_alert_policy.west_job_failed) == 1 && google_monitoring_alert_policy.west_pubsub_backlog[0].conditions[0].condition_threshold[0].threshold_value == 86400
    error_message = "Failure and24h backlog policies must be consolidated."
  }
}
