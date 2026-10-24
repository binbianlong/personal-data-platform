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
  secret_versions                = { motherduck_token = "7", fitbit_oauth_config = "11", fitbit_webhook_config = "13", heartbeat_config = "17", owntracks_config = "19" }
}

run "pull_batches_existing_subscription" {
  command = plan
  override_resource {
    target          = google_cloud_run_v2_service.fitbit_worker
    override_during = plan
    values          = { uri = "https://worker.example.run.app" }
  }
  override_resource {
    target          = google_service_account.scheduler
    override_during = plan
    values          = { email = "scheduler@example-project.iam.gserviceaccount.com", name = "projects/example-project/serviceAccounts/scheduler@example-project.iam.gserviceaccount.com" }
  }
  override_resource {
    target          = google_service_account.processing
    override_during = plan
    values          = { email = "runtime@example-project.iam.gserviceaccount.com", name = "projects/example-project/serviceAccounts/runtime@example-project.iam.gserviceaccount.com" }
  }
  assert {
    condition     = toset(keys(output.runtime_jobs)) == toset(["daily"]) && toset(keys(output.scheduler_jobs)) == toset(["daily", "fitbit_batch"])
    error_message = "Batch notification ingestion must add a Scheduler without another Cloud Run Job."
  }
  assert {
    condition     = google_cloud_run_v2_service.fitbit_worker.template[0].max_instance_request_concurrency == 1 && google_cloud_run_v2_service.fitbit_worker.template[0].scaling[0].min_instance_count == 0 && google_cloud_run_v2_service.fitbit_worker.template[0].scaling[0].max_instance_count == 1 && google_cloud_run_v2_service.fitbit_worker.template[0].timeout == "540s"
    error_message = "Batch processing must be serialized, scale to zero, and finish before acknowledgement expiry."
  }
  assert {
    condition     = google_cloud_run_v2_service_iam_member.fitbit_worker.member == "serviceAccount:${google_service_account.scheduler.email}" && google_cloud_run_v2_service_iam_member.fitbit_worker.role == "roles/run.invoker" && !contains(keys(local.runtime_common_environment), "GCS_BUCKET")
    error_message = "Only the trigger identity may invoke the worker and ingestion must not require GCS."
  }
  assert {
    condition     = google_pubsub_subscription.fitbit.name == "pdp-fitbit-west-pull" && google_pubsub_topic.fitbit.name == "pdp-fitbit-west" && length(google_pubsub_subscription.fitbit.push_config) == 0 && google_pubsub_subscription.fitbit.filter == null && google_pubsub_subscription.fitbit.ack_deadline_seconds == 600 && google_pubsub_subscription.fitbit.message_retention_duration == "604800s"
    error_message = "Scheduled batches must retain the existing unfiltered queue with its acknowledgement deadline and seven-day retention."
  }
  assert {
    condition     = google_cloud_scheduler_job.fitbit_batch.name == "pdp-fitbit-batch-west" && !google_cloud_scheduler_job.fitbit_batch.paused && google_cloud_scheduler_job.fitbit_batch.schedule == "0 * * * *" && google_cloud_scheduler_job.fitbit_batch.time_zone == "Asia/Tokyo" && google_cloud_scheduler_job.fitbit_batch.attempt_deadline == "540s" && google_cloud_scheduler_job.fitbit_batch.retry_config[0].retry_count == 0
    error_message = "Pull delivery needs an active hourly Scheduler with bounded execution and no Scheduler retries."
  }
  assert {
    condition     = google_cloud_scheduler_job.fitbit_batch.http_target[0].http_method == "POST" && google_cloud_scheduler_job.fitbit_batch.http_target[0].uri == "https://worker.example.run.app/notifications/fitbit/drain" && base64decode(google_cloud_scheduler_job.fitbit_batch.http_target[0].body) == "{}" && google_cloud_scheduler_job.fitbit_batch.http_target[0].headers["Content-Type"] == "application/json" && google_cloud_scheduler_job.fitbit_batch.http_target[0].oidc_token[0].service_account_email == "scheduler@example-project.iam.gserviceaccount.com" && google_cloud_scheduler_job.fitbit_batch.http_target[0].oidc_token[0].audience == "https://worker.example.run.app"
    error_message = "The batch Scheduler must invoke the private drain endpoint using the existing Scheduler identity and worker audience."
  }
  assert {
    condition     = google_pubsub_subscription_iam_member.fitbit_worker.subscription == google_pubsub_subscription.fitbit.name && google_pubsub_subscription_iam_member.fitbit_worker.role == "roles/pubsub.subscriber" && google_pubsub_subscription_iam_member.fitbit_worker.member == "serviceAccount:runtime@example-project.iam.gserviceaccount.com"
    error_message = "Only the existing worker runtime identity must receive subscription-scoped pull access."
  }
  assert {
    condition     = length(google_pubsub_subscription.owntracks.push_config) == 1 && google_cloud_scheduler_job.runtime["daily"].schedule == "10 4 * * *" && !google_cloud_scheduler_job.runtime["daily"].paused && local.runtime_common_environment.PDP_FITBIT_PROCESSING_PAUSED == "false" && google_cloud_run_v2_service.owntracks["worker"].template[0].containers[0].image == var.runtime_image_uri && google_cloud_run_v2_job.runtime["daily"].template[0].template[0].containers[0].image == var.runtime_image_uri
    error_message = "Fitbit batches must preserve OwnTracks push, the daily schedule, processing state, and runtime image pins."
  }
  assert {
    condition     = output.scheduler_jobs.fitbit_batch.name == "pdp-fitbit-batch-west" && output.runtime_resources.fitbit_drain_url == "https://worker.example.run.app/notifications/fitbit/drain"
    error_message = "Operators need the batch schedule and drain endpoint in runtime outputs."
  }
  assert {
    condition     = google_monitoring_alert_policy.pubsub_backlog.conditions[0].condition_threshold[0].threshold_value == 4500
    error_message = "Hourly Fitbit batches need a 75-minute backlog threshold."
  }
}

run "paused_pull_keeps_queue_and_stops_batch_scheduler" {
  command = plan
  variables { schedulers_enabled = false }
  assert {
    condition     = google_cloud_scheduler_job.fitbit_batch.paused && length(google_pubsub_subscription.fitbit.push_config) == 0 && local.runtime_common_environment.PDP_FITBIT_PROCESSING_PAUSED == "true"
    error_message = "The global pause must stop the batch schedule without restoring push delivery."
  }
}
