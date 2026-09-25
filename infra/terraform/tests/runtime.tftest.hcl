mock_provider "google" {
  override_during = plan
}

variables {
  project_id                     = "example-project"
  deployer_service_account_email = "github-tf-deploy@example-project.iam.gserviceaccount.com"
  collector_impersonator_member  = "user:operator@example.com"
  image_uri                      = "us-central1-docker.pkg.dev/example-project/personal-data-platform/personal-data-platform@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  alert_email                    = "operator@example.com"
}

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
    condition     = local.scheduled_jobs.reconciliation.schedule == "30 4,16 * * *"
    error_message = "Reconciliation must run at 04:30 and 16:30."
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
      toset(one(one(google_storage_bucket.raw.lifecycle_rule).condition).matches_prefix) == toset(["raw/screen_time/v1/", "raw/screen_time/v2/"]),
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

run "additional_source_and_stream_contract" {
  command = plan

  override_resource {
    target          = google_service_account.collector
    override_during = plan
    values = {
      email = "screen-time-collector@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/screen-time-collector@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.rebuild_operator
    override_during = plan
    values = {
      email = "raw-rebuild-operator@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/raw-rebuild-operator@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["preflight"]
    override_during = plan
    values = {
      email = "preflight@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/preflight@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["dbt"]
    override_during = plan
    values = {
      email = "dbt-runner@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/dbt-runner@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["loader"]
    override_during = plan
    values = {
      email = "loader@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/loader@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["reconciliation"]
    override_during = plan
    values = {
      email = "reconciliation@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/reconciliation@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["fixture_steps_loader"]
    override_during = plan
    values = {
      email = "fixture-steps-loader@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/fixture-steps-loader@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["fixture_steps_reconciliation"]
    override_during = plan
    values = {
      email = "fixture-steps-reconciliation@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/fixture-steps-reconciliation@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["fixture_sleep_loader"]
    override_during = plan
    values = {
      email = "fixture-sleep-loader@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/fixture-sleep-loader@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["fixture_sleep_reconciliation"]
    override_during = plan
    values = {
      email = "fixture-sleep-reconciliation@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/fixture-sleep-reconciliation@example-project.iam.gserviceaccount.com"
    }
  }

  variables {
    additional_ingestion_pipelines = {
      fixture_steps = {
        source_id               = "fixture_health"
        stream                  = "steps"
        raw_prefixes            = ["raw/fixture_health/v1/", "raw/fixture_health/v2/"]
        raw_suffixes            = [".json.gz"]
        retention_days          = 30
        lifecycle_grace_days    = 2
        loader_schedule         = "5 */2 * * *"
        reconciliation_schedule = "0 6 * * *"
        raw_creator_members     = ["serviceAccount:fixture-steps@example-project.iam.gserviceaccount.com"]
      }
      fixture_sleep = {
        source_id               = "fixture_health"
        stream                  = "sleep"
        raw_prefixes            = ["raw/fixture_health/v1/", "raw/fixture_health/v2/"]
        raw_suffixes            = [".json.gz"]
        retention_days          = 30
        lifecycle_grace_days    = 2
        loader_schedule         = "25 */3 * * *"
        reconciliation_schedule = "30 6 * * *"
        raw_creator_members     = ["serviceAccount:fixture-sleep@example-project.iam.gserviceaccount.com"]
      }
    }
  }

  assert {
    condition = alltrue([
      length(google_cloud_run_v2_job.runtime) == 8,
      length(google_cloud_scheduler_job.runtime) == 6,
      length(toset([for account in values(google_service_account.runtime) : account.account_id])) == 8,
      google_cloud_run_v2_job.runtime["loader"].name == "screen-time-loader",
      google_cloud_run_v2_job.runtime["reconciliation"].name == "reconciliation",
      google_cloud_scheduler_job.runtime["loader"].name == "screen-time-loader-hourly",
      google_cloud_scheduler_job.runtime["reconciliation"].name == "reconciliation-daily",
    ])
    error_message = "Additional source streams need independent Jobs and identities while preserving existing resource names."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_job.runtime["fixture_steps_loader"].template[0].template[0].containers[0].args == tolist(["loader", "--source", "fixture_health", "--stream", "steps"]),
      google_cloud_run_v2_job.runtime["fixture_sleep_reconciliation"].template[0].template[0].containers[0].args == tolist(["reconciliation", "--source", "fixture_health", "--stream", "sleep"]),
      alltrue([for job in values(google_cloud_run_v2_job.runtime) : job.template[0].task_count == 1 && job.template[0].parallelism == 1]),
      alltrue([for job in values(google_cloud_run_v2_job.runtime) : job.template[0].template[0].containers[0].image == var.image_uri]),
      alltrue([for key, job in google_cloud_run_v2_job.runtime : job.template[0].template[0].service_account == google_service_account.runtime[key].email]),
    ])
    error_message = "Every added Job must select its source/stream, retain serialized execution, and use its dedicated identity."
  }

  assert {
    condition = alltrue([
      local.ingestion_jobs.fixture_steps_loader.environment.PDP_RAW_RETENTION_DAYS == "30",
      local.ingestion_jobs.fixture_steps_reconciliation.environment.PDP_LIFECYCLE_GRACE_DAYS == "2",
      jsondecode(local.ingestion_jobs.fixture_sleep_loader.environment.PDP_RAW_PREFIXES_JSON) == ["raw/fixture_health/v1/", "raw/fixture_health/v2/"],
      jsondecode(local.ingestion_jobs.fixture_sleep_loader.environment.PDP_RAW_SUFFIXES_JSON) == [".json.gz"],
      alltrue([for job in values(local.ingestion_jobs) : job.environment.GCS_BUCKET == "example-project-pdp-raw"]),
    ])
    error_message = "The runtime must receive the same namespace, versions, and retention policy used by storage provisioning."
  }

  assert {
    condition = alltrue([
      google_cloud_scheduler_job.runtime["fixture_steps_loader"].schedule == "5 */2 * * *",
      google_cloud_scheduler_job.runtime["fixture_sleep_loader"].schedule == "25 */3 * * *",
      google_cloud_scheduler_job.runtime["fixture_steps_reconciliation"].schedule == "0 6 * * *",
      google_cloud_scheduler_job.runtime["fixture_sleep_reconciliation"].schedule == "30 6 * * *",
      alltrue([for job in values(google_cloud_scheduler_job.runtime) : job.time_zone == "Asia/Tokyo"]),
      alltrue([for key, binding in google_cloud_run_v2_job_iam_member.scheduler : binding.role == "roles/run.invoker" && binding.name == google_cloud_run_v2_job.runtime[key].name]),
    ])
    error_message = "Each stream needs its own schedule and invoker binding."
  }

  assert {
    condition = alltrue([
      length(google_secret_manager_secret.runtime) == 5,
      google_secret_manager_secret.runtime["healthchecks_ping_url"].secret_id == "healthchecks-ping-url",
      google_secret_manager_secret.runtime["fixture_steps_heartbeat"].secret_id == "pdp-fixture-steps-heartbeat",
      google_secret_manager_secret.runtime["fixture_sleep_heartbeat"].secret_id == "pdp-fixture-sleep-heartbeat",
      local.runtime_jobs.fixture_steps_reconciliation.secrets.RECONCILIATION_HEARTBEAT_URL == "fixture_steps_heartbeat",
      local.runtime_jobs.fixture_sleep_reconciliation.secrets.RECONCILIATION_HEARTBEAT_URL == "fixture_sleep_heartbeat",
      local.runtime_jobs.reconciliation.environment.PDP_RECONCILIATION_MONITORING_MODE == "cloud_monitoring",
      !contains(keys(local.runtime_jobs.reconciliation.secrets), "RECONCILIATION_HEARTBEAT_URL"),
      google_monitoring_alert_policy.reconciliation_absent.conditions[0].condition_absent[0].duration == "84600s",
      google_monitoring_alert_policy.reconciliation_absent.conditions[0].condition_absent[0].aggregations[0].cross_series_reducer == "REDUCE_SUM",
      contains(keys(google_secret_manager_secret_iam_member.runtime), "fixture_steps_reconciliation:fixture_steps_heartbeat"),
      !contains(keys(google_secret_manager_secret_iam_member.runtime), "fixture_sleep_reconciliation:fixture_steps_heartbeat"),
      !contains(keys(google_secret_manager_secret_iam_member.runtime), "fixture_steps_loader:fixture_steps_heartbeat"),
      length(google_monitoring_alert_policy.job_failed) == 8,
      length(google_monitoring_alert_policy.job_error_log) == 8,
    ])
    error_message = "Heartbeat secrets and monitor identities must not be shared between source streams or exposed to loaders."
  }

  assert {
    condition = alltrue([
      anytrue([for rule in google_storage_bucket.raw.lifecycle_rule : one(rule.condition).age == 90 && toset(one(rule.condition).matches_prefix) == toset(["raw/screen_time/v1/", "raw/screen_time/v2/"]) && toset(one(rule.condition).matches_suffix) == toset([".segb.gz"])]),
      anytrue([for rule in google_storage_bucket.raw.lifecycle_rule : one(rule.condition).age == 30 && toset(one(rule.condition).matches_prefix) == toset(["raw/fixture_health/v1/", "raw/fixture_health/v2/"]) && toset(one(rule.condition).matches_suffix) == toset([".json.gz"])]),
      alltrue([for rule in google_storage_bucket.raw.lifecycle_rule : one(rule.action).type == "Delete"]),
    ])
    error_message = "Each source namespace must retain its configured lifecycle, including every supported Raw schema version."
  }

  assert {
    condition = alltrue([
      alltrue([
        for key in keys(local.ingestion_jobs) : contains(
          one([for binding in data.google_iam_policy.raw_bucket.binding : binding if binding.role == "roles/storage.objectViewer"]).members,
          "serviceAccount:${google_service_account.runtime[key].email}"
        )
      ]),
      !contains(one([for binding in data.google_iam_policy.raw_bucket.binding : binding if binding.role == "roles/storage.objectViewer"]).members, "serviceAccount:${google_service_account.runtime["dbt"].email}"),
      !contains(one([for binding in data.google_iam_policy.raw_bucket.binding : binding if binding.role == "roles/storage.objectViewer"]).members, "serviceAccount:${google_service_account.runtime["preflight"].email}"),
      one([for binding in data.google_iam_policy.raw_bucket.binding : binding if try(one(binding.condition).title, "") == "fixture_steps_raw_create_only"]).members == toset(["serviceAccount:fixture-steps@example-project.iam.gserviceaccount.com"]),
      one(one([for binding in data.google_iam_policy.raw_bucket.binding : binding if try(one(binding.condition).title, "") == "fixture_steps_raw_create_only"]).condition).expression == "(resource.name.startsWith('projects/_/buckets/example-project-pdp-raw/objects/raw/fixture_health/v1/') || resource.name.startsWith('projects/_/buckets/example-project-pdp-raw/objects/raw/fixture_health/v2/')) && (resource.name.endsWith('.json.gz'))",
    ])
    error_message = "Added acquisition principals must have namespace-limited create access; only ingestion and rebuild identities receive Raw read access."
  }
}

run "reject_duplicate_source_stream" {
  command = plan

  variables {
    additional_ingestion_pipelines = {
      duplicate = {
        source_id               = "screen_time"
        stream                  = "app-in-focus"
        raw_prefixes            = ["raw/screen_time/v1/"]
        raw_suffixes            = [".segb.gz"]
        retention_days          = 90
        lifecycle_grace_days    = 3
        loader_schedule         = "5 * * * *"
        reconciliation_schedule = "0 6 * * *"
        raw_creator_members     = []
      }
    }
  }

  expect_failures = [google_storage_bucket.raw]
}

run "reject_shared_namespace_with_different_retention" {
  command = plan

  variables {
    additional_ingestion_pipelines = {
      screen_mac = {
        source_id               = "screen_time"
        stream                  = "app-usage"
        raw_prefixes            = ["raw/screen_time/v1/"]
        raw_suffixes            = [".segb.gz"]
        retention_days          = 30
        lifecycle_grace_days    = 3
        loader_schedule         = "5 * * * *"
        reconciliation_schedule = "0 6 * * *"
        raw_creator_members     = []
      }
    }
  }

  expect_failures = [google_storage_bucket.raw]
}

run "reject_nested_namespace_with_different_retention" {
  command = plan

  variables {
    additional_ingestion_pipelines = {
      screen_mac = {
        source_id               = "screen_time"
        stream                  = "app-usage"
        raw_prefixes            = ["raw/screen_time/v1/local/"]
        raw_suffixes            = [".segb.gz"]
        retention_days          = 30
        lifecycle_grace_days    = 3
        loader_schedule         = "5 * * * *"
        reconciliation_schedule = "0 6 * * *"
        raw_creator_members     = []
      }
    }
  }

  expect_failures = [google_storage_bucket.raw]
}

run "reject_noncanonical_namespace" {
  command = plan

  variables {
    additional_ingestion_pipelines = {
      fixture_steps = {
        source_id               = "fixture_health"
        stream                  = "steps"
        raw_prefixes            = ["raw/fixture_health/v1/'/"]
        raw_suffixes            = [".json.gz"]
        retention_days          = 30
        lifecycle_grace_days    = 2
        loader_schedule         = "5 * * * *"
        reconciliation_schedule = "0 6 * * *"
        raw_creator_members     = []
      }
    }
  }

  expect_failures = [var.additional_ingestion_pipelines]
}

run "ingestion_has_read_only_raw_access" {
  command = plan

  override_resource {
    target          = google_service_account.runtime["loader"]
    override_during = plan
    values = {
      email = "loader@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/loader@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["reconciliation"]
    override_during = plan
    values = {
      email = "reconciliation@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/reconciliation@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["dbt"]
    override_during = plan
    values = {
      email = "pdp-dbt@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/pdp-dbt@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.runtime["preflight"]
    override_during = plan
    values = {
      email = "preflight@example-project.iam.gserviceaccount.com"
      name  = "projects/example-project/serviceAccounts/preflight@example-project.iam.gserviceaccount.com"
    }
  }

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

  assert {
    condition = alltrue([
      for binding in data.google_iam_policy.raw_bucket.binding :
      binding.role == "roles/storage.objectViewer"
      if length(setintersection(toset(binding.members), toset([
        "serviceAccount:${google_service_account.runtime["loader"].email}",
        "serviceAccount:${google_service_account.runtime["reconciliation"].email}",
      ]))) > 0
    ])
    error_message = "Loader and Reconciliation must have read-only access to the Raw bucket."
  }
}
