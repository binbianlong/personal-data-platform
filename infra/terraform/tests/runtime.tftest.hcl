mock_provider "google" {}

run "runtime_contract" {
  command = plan

  variables {
    project_id                     = "example-project"
    deployer_service_account_email = "github-tf-deploy@example-project.iam.gserviceaccount.com"
    image_uri                      = "asia-northeast1-docker.pkg.dev/example-project/personal-data-platform/personal-data-platform@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
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
    condition     = local.scheduled_jobs.reconciliation.schedule == "30 4 * * *"
    error_message = "Reconciliation must run daily at 04:30."
  }

  assert {
    condition     = var.scheduler_time_zone == "Asia/Tokyo"
    error_message = "Runtime schedules must use Asia/Tokyo."
  }

  assert {
    condition     = alltrue([for binding in values(google_cloud_run_v2_job_iam_member.scheduler) : binding.role == "roles/run.invoker"])
    error_message = "The scheduler identity must receive only the Cloud Run invoker role on scheduled Jobs."
  }

  assert {
    condition     = length(google_secret_manager_secret.runtime) == 9
    error_message = "Runtime Terraform must create the nine named secret containers."
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
    image_uri                      = "asia-northeast1-docker.pkg.dev/example-project/personal-data-platform/personal-data-platform:latest"
    alert_email                    = "operator@example.com"
  }

  expect_failures = [var.image_uri]
}
