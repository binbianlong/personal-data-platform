locals {
  b2_secret_environment = {
    B2_KEY_ID          = "b2_key_id"
    B2_APPLICATION_KEY = "b2_application_key"
    B2_ENDPOINT        = "b2_endpoint"
    B2_BUCKET          = "b2_bucket"
  }

  runtime_jobs = {
    preflight = {
      name        = "platform-preflight"
      role        = "preflight"
      timeout     = "600s"
      max_retries = 0
      resources = {
        cpu    = "1"
        memory = "512Mi"
      }
      environment = {
        APP_ENV                       = "production"
        ANALYTICS_TIME_ZONE           = var.analytics_time_zone
        B2_RAW_PREFIX                 = var.preflight_b2_prefix
        MOTHERDUCK_DATABASE           = var.motherduck_database
        PREFLIGHT_MOTHERDUCK_DATABASE = var.preflight_motherduck_database
      }
      secrets = {
        B2_KEY_ID          = "preflight_b2_key_id"
        B2_APPLICATION_KEY = "preflight_b2_application_key"
        B2_ENDPOINT        = "b2_endpoint"
        B2_BUCKET          = "b2_bucket"
        MOTHERDUCK_TOKEN   = "motherduck_preflight_token"
      }
    }
    loader = {
      name        = "screen-time-loader"
      role        = "loader"
      timeout     = "3600s"
      max_retries = 2
      resources = {
        cpu    = "1"
        memory = "1Gi"
      }
      environment = {
        APP_ENV             = "production"
        ANALYTICS_TIME_ZONE = var.analytics_time_zone
        B2_RAW_PREFIX       = var.b2_raw_prefix
        MOTHERDUCK_DATABASE = var.motherduck_database
      }
      secrets = merge(local.b2_secret_environment, {
        MOTHERDUCK_TOKEN = "motherduck_token"
      })
    }
    dbt = {
      name        = "dbt-runner"
      role        = "dbt"
      timeout     = "1800s"
      max_retries = 1
      resources = {
        cpu    = "1"
        memory = "1Gi"
      }
      environment = {
        APP_ENV             = "production"
        ANALYTICS_TIME_ZONE = var.analytics_time_zone
        MOTHERDUCK_DATABASE = var.motherduck_database
      }
      secrets = {
        MOTHERDUCK_TOKEN = "motherduck_token"
      }
    }
    reconciliation = {
      name        = "reconciliation"
      role        = "reconciliation"
      timeout     = "3600s"
      max_retries = 1
      resources = {
        cpu    = "1"
        memory = "1Gi"
      }
      environment = {
        APP_ENV             = "production"
        ANALYTICS_TIME_ZONE = var.analytics_time_zone
        B2_RAW_PREFIX       = var.b2_raw_prefix
        MOTHERDUCK_DATABASE = var.motherduck_database
      }
      secrets = merge(local.b2_secret_environment, {
        RECONCILIATION_HEARTBEAT_URL = "healthchecks_ping_url"
        MOTHERDUCK_TOKEN             = "motherduck_token"
      })
    }
  }

  job_secret_access = merge([
    for job_key, job in local.runtime_jobs : {
      for secret_key in toset(values(job.secrets)) :
      "${job_key}:${secret_key}" => {
        job_key    = job_key
        secret_key = secret_key
      }
    }
  ]...)
}

resource "google_service_account" "runtime" {
  for_each = local.runtime_jobs

  project      = var.project_id
  account_id   = each.value.name
  display_name = "personal-data-platform ${each.key}"
  description  = "Dedicated runtime identity for the ${each.value.name} Cloud Run Job"

  depends_on = [google_project_service.runtime]
}

resource "google_service_account_iam_member" "deployer_act_as_runtime" {
  for_each = google_service_account.runtime

  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}

resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each = local.job_secret_access

  project   = var.project_id
  secret_id = google_secret_manager_secret.runtime[each.value.secret_key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.value.job_key].email}"
}

resource "google_cloud_run_v2_job" "runtime" {
  for_each = local.runtime_jobs

  project             = var.project_id
  location            = var.region
  name                = each.value.name
  deletion_protection = false

  labels = {
    application = "personal-data-platform"
    role        = each.key
  }

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account       = google_service_account.runtime[each.key].email
      timeout               = each.value.timeout
      max_retries           = each.value.max_retries
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      containers {
        name  = each.value.name
        image = var.image_uri
        args  = [each.value.role]

        resources {
          limits = each.value.resources
        }

        dynamic "env" {
          for_each = each.value.environment

          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = each.value.secrets

          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.runtime[env.value].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_service_account_iam_member.deployer_act_as_runtime,
    google_project_service.runtime,
    google_secret_manager_secret_iam_member.runtime,
  ]
}
