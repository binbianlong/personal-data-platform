mock_provider "google" {}

run "runtime_contract" {
  command = plan

  variables {
    project_id                     = "example-project"
    deployer_service_account_email = "github-tf-deploy@example-project.iam.gserviceaccount.com"
    collector_impersonator_member  = "user:operator@example.com"
    image_uri                      = "us-central1-docker.pkg.dev/example-project/personal-data-platform/personal-data-platform@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    alert_email                    = "operator@example.com"
  }

  assert {
    condition     = toset(keys(local.runtime_jobs)) == toset(["preflight", "loader", "dbt", "reconciliation"])
    error_message = "The runtime must contain exactly the four approved Cloud Run Jobs."
  }

  assert {
    condition     = alltrue([for job in values(google_cloud_run_v2_job.runtime) : job.template[0].task_count == 1 && job.template[0].parallelism == 1])
    error_message = "Every Cloud Run Job must execute one task with parallelism one."
  }

  assert {
    condition     = alltrue([for job in values(google_cloud_run_v2_job.runtime) : job.template[0].template[0].containers[0].image == var.image_uri])
    error_message = "Every Cloud Run Job must use the supplied immutable digest."
  }

  assert {
    condition     = length(toset([for account in values(google_service_account.runtime) : account.account_id])) == 4
    error_message = "Every Cloud Run Job must have a distinct Service Account."
  }

  assert {
    condition     = alltrue([for binding in values(google_service_account_iam_member.deployer_act_as_runtime) : binding.role == "roles/iam.serviceAccountUser"])
    error_message = "The deployer must receive act-as only on the dedicated runtime identities."
  }

  assert {
    condition     = local.scheduled_jobs.loader.schedule == "15 * * * *"
    error_message = "The loader must run at minute 15 of every hour."
  }

  assert {
    condition = alltrue([
      local.runtime_jobs.loader.environment.GCS_BUCKET == "example-project-pdp-raw",
      local.runtime_jobs.reconciliation.environment.GCS_BUCKET == "example-project-pdp-raw",
      local.runtime_jobs.preflight.environment.GCS_BUCKET == "example-project-pdp-raw",
      local.runtime_jobs.preflight.environment.GCS_PREFLIGHT_BUCKET == "example-project-pdp-preflight",
      alltrue([for job in values(local.runtime_jobs) : alltrue([for name in keys(job.environment) : !startswith(name, "B2_")])]),
    ])
    error_message = "Storage Jobs must receive only the fixed production and isolated preflight GCS buckets."
  }

  assert {
    condition     = local.scheduled_jobs.reconciliation.schedule == "30 4 * * *"
    error_message = "Reconciliation must run daily at 04:30."
  }

  assert {
    condition     = var.scheduler_time_zone == "Asia/Tokyo"
    error_message = "Runtime schedules must use Asia/Tokyo."
  }

  assert {
    condition     = alltrue([for job in values(local.runtime_jobs) : !contains(keys(job.environment), "ANALYTICS_TIME_ZONE")])
    error_message = "Runtime jobs must not expose an unsupported analytics time zone override."
  }

  assert {
    condition     = alltrue([for binding in values(google_cloud_run_v2_job_iam_member.scheduler) : binding.role == "roles/run.invoker"])
    error_message = "The scheduler identity must receive only the Cloud Run invoker role on scheduled Jobs."
  }

  assert {
    condition = alltrue([
      length(local.runtime_secrets) == 3,
      alltrue([for job in values(local.runtime_jobs) : alltrue([for secret in values(job.secrets) : !startswith(secret, "b2_") && !startswith(secret, "preflight_b2_")])]),
    ])
    error_message = "Only the three active non-B2 secrets may be created or injected."
  }

  assert {
    condition = alltrue([
      google_service_account.collector.account_id == "screen-time-collector",
      google_service_account_iam_member.collector_impersonator.role == "roles/iam.serviceAccountTokenCreator",
      google_service_account_iam_member.collector_impersonator.member == "user:operator@example.com",
      google_service_account.rebuild_operator.account_id == "raw-rebuild-operator",
      google_service_account_iam_member.rebuild_operator_impersonator.role == "roles/iam.serviceAccountTokenCreator",
      google_service_account_iam_member.rebuild_operator_impersonator.member == "user:operator@example.com",
    ])
    error_message = "The configured operator must be able to impersonate the separate write-only Collector and read-only rebuild identities."
  }

  assert {
    condition = alltrue([
      google_storage_bucket.raw.name == "example-project-pdp-raw",
      google_storage_bucket.raw.location == "us-central1",
      google_storage_bucket.raw.storage_class == "STANDARD",
      google_storage_bucket.raw.uniform_bucket_level_access,
      google_storage_bucket.raw.public_access_prevention == "enforced",
      !google_storage_bucket.raw.force_destroy,
      google_storage_bucket.raw.soft_delete_policy[0].retention_duration_seconds == 0,
      one(one(google_storage_bucket.raw.lifecycle_rule).action).type == "Delete",
      one(one(google_storage_bucket.raw.lifecycle_rule).condition).age == 90,
      toset(one(one(google_storage_bucket.raw.lifecycle_rule).condition).matches_prefix) == toset(["raw/screen_time/v1/"]),
      toset(one(one(google_storage_bucket.raw.lifecycle_rule).condition).matches_suffix) == toset([".segb.gz"]),
      length(google_storage_bucket.raw.versioning) == 0,
      length(google_storage_bucket.raw.autoclass) == 0,
    ])
    error_message = "Raw storage must be a protected Standard bucket with permanent 90-day segment deletion."
  }

  assert {
    condition = alltrue([
      google_storage_bucket.preflight.name == "example-project-pdp-preflight",
      google_storage_bucket.preflight.storage_class == "STANDARD",
      google_storage_bucket.preflight.soft_delete_policy[0].retention_duration_seconds == 0,
      one(one(google_storage_bucket.preflight.lifecycle_rule).action).type == "Delete",
      one(one(google_storage_bucket.preflight.lifecycle_rule).condition).age == 1,
      toset(one(one(google_storage_bucket.preflight.lifecycle_rule).condition).matches_prefix) == toset(["test/preflight/"]),
    ])
    error_message = "Preflight storage must be isolated and clean up orphan test objects after one day."
  }

  assert {
    condition     = length(google_monitoring_alert_policy.job_failed) == 4 && length(google_monitoring_alert_policy.job_error_log) == 4
    error_message = "Every Cloud Run Job must alert on failed executions and ERROR logs."
  }
}

run "reject_mutable_image_tag" {
  command = plan

  variables {
    project_id                     = "example-project"
    deployer_service_account_email = "github-tf-deploy@example-project.iam.gserviceaccount.com"
    collector_impersonator_member  = "user:operator@example.com"
    image_uri                      = "us-central1-docker.pkg.dev/example-project/personal-data-platform/personal-data-platform:latest"
    alert_email                    = "operator@example.com"
  }

  expect_failures = [var.image_uri]
}
