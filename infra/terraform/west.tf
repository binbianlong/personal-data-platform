locals {
  west_raw_bucket_name       = "${var.project_id}-pdp-raw-west"
  west_preflight_bucket_name = "${var.project_id}-pdp-preflight-west"
  west_common_environment = {
    APP_ENV                        = "production"
    GOOGLE_CLOUD_PROJECT           = var.project_id
    MOTHERDUCK_DATABASE            = var.west_motherduck_database
    PDP_FITBIT_SUBJECT_KEY         = var.fitbit_subject_key
    PDP_FITBIT_PROCESSING_PAUSED   = tostring(!var.west_schedulers_enabled)
    PDP_FITBIT_PUBSUB_SUBSCRIPTION = "projects/${var.project_id}/subscriptions/pdp-fitbit-west-pull"
  }
  west_jobs = {
    daily = {
      name        = "reconciliation-west"
      args        = ["reconciliation"]
      timeout     = "6000s"
      environment = local.west_common_environment
      secrets     = { MOTHERDUCK_TOKEN = "motherduck_token", PDP_FITBIT_OAUTH_CONFIG = "fitbit_oauth_config", PDP_HEARTBEAT_CONFIG = "heartbeat_config" }
    }
  }
  west_job_secret_access = { for key in toset(flatten([for job in values(local.west_jobs) : values(job.secrets)])) : key => key }
  west_scheduled_jobs    = { daily = "10 4 * * *" }
}

resource "google_storage_bucket" "raw_west" {
  name                        = local.west_raw_bucket_name
  project                     = var.project_id
  location                    = var.west_region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  soft_delete_policy { retention_duration_seconds = 0 }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.runtime]
}
resource "google_storage_bucket" "preflight_west" {
  name                        = local.west_preflight_bucket_name
  project                     = var.project_id
  location                    = var.west_region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  lifecycle_rule {
    action { type = "Delete" }
    condition {
      age            = 1
      matches_prefix = ["test/preflight/"]
    }
  }
  soft_delete_policy { retention_duration_seconds = 0 }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.runtime]
}
resource "google_service_account" "west" {
  project      = var.project_id
  account_id   = "pdp-runtime-west"
  display_name = "PDP west processing"
  depends_on   = [google_project_service.runtime]
}
resource "google_service_account_iam_member" "west_deployer" {
  service_account_id = google_service_account.west.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}
resource "google_secret_manager_secret_iam_member" "west_job" {
  for_each  = local.west_job_secret_access
  project   = var.project_id
  secret_id = google_secret_manager_secret.west[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.west.email}"
}
resource "google_cloud_run_v2_job" "west" {
  for_each            = local.west_jobs
  project             = var.project_id
  location            = var.west_region
  name                = each.value.name
  deletion_protection = true
  labels              = { application = "personal-data-platform", role = each.key }
  template {
    parallelism = 1
    task_count  = 1
    template {
      service_account = google_service_account.west.email
      timeout         = each.value.timeout
      max_retries     = 0
      containers {
        image = var.west_image_uri
        args  = each.value.args
        resources { limits = { cpu = "1", memory = "2Gi" } }
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
                secret  = google_secret_manager_secret.west[env.value].secret_id
                version = var.west_secret_versions[env.value]
              }
            }
          }
        }
      }
    }
  }
  depends_on = [google_service_account_iam_member.west_deployer, google_secret_manager_secret_iam_member.west_job, google_storage_bucket_iam_policy.raw_west, google_storage_bucket_iam_policy.preflight_west]
}
resource "google_service_account" "west_receiver" {
  project      = var.project_id
  account_id   = "pdp-fitbit-receiver-west"
  display_name = "PDP west notification publisher"
  depends_on   = [google_project_service.runtime]
}
resource "google_service_account_iam_member" "west_receiver_deployer" {
  service_account_id = google_service_account.west_receiver.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}
resource "google_secret_manager_secret_iam_member" "west_receiver" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.west["fitbit_webhook_config"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.west_receiver.email}"
}
resource "google_cloud_run_v2_service" "west" {
  project             = var.project_id
  location            = var.west_region
  name                = "pdp-fitbit-west"
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = { application = "personal-data-platform", role = "receiver" }
  template {
    service_account                  = google_service_account.west_receiver.email
    timeout                          = "60s"
    max_instance_request_concurrency = 16
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = var.west_image_uri
      args  = ["fitbit", "serve"]
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true
      }
      dynamic "env" {
        for_each = {
          GOOGLE_CLOUD_PROJECT       = var.project_id
          PDP_FITBIT_PUBSUB_ENDPOINT = "pubsub.us-west1.rep.googleapis.com"
          PDP_FITBIT_PUBSUB_TOPIC    = "projects/${var.project_id}/topics/pdp-fitbit-west"
          PDP_FITBIT_SUBJECT_KEY     = var.fitbit_subject_key
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = { PDP_FITBIT_WEBHOOK_CONFIG = "fitbit_webhook_config" }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.west[env.value].secret_id
              version = var.west_secret_versions[env.value]
            }
          }
        }
      }
    }
  }
  depends_on = [google_service_account_iam_member.west_receiver_deployer, google_secret_manager_secret_iam_member.west_receiver, google_pubsub_topic_iam_member.west_receiver]
}
resource "google_cloud_run_v2_service_iam_member" "west_public" {
  project  = var.project_id
  location = var.west_region
  name     = google_cloud_run_v2_service.west.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
resource "google_service_account" "west_scheduler" {
  project      = var.project_id
  account_id   = "scheduler-invoker-west"
  display_name = "PDP west Scheduler invoker"
  depends_on   = [google_project_service.runtime]
}
resource "google_service_account_iam_member" "west_scheduler_deployer" {
  service_account_id = google_service_account.west_scheduler.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}
resource "google_cloud_run_v2_job_iam_member" "west_scheduler" {
  for_each = local.west_scheduled_jobs
  project  = var.project_id
  location = var.west_region
  name     = google_cloud_run_v2_job.west[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.west_scheduler.email}"
}
resource "google_cloud_scheduler_job" "west" {
  for_each         = local.west_scheduled_jobs
  project          = var.project_id
  region           = var.west_region
  name             = "pdp-${each.key}-west"
  schedule         = each.value
  time_zone        = var.scheduler_time_zone
  paused           = !var.west_schedulers_enabled
  attempt_deadline = "320s"
  retry_config { retry_count = 0 }
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.west_region}/jobs/${google_cloud_run_v2_job.west[each.key].name}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = google_service_account.west_scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.west_scheduler, google_service_account_iam_member.west_scheduler_deployer]
}

data "google_iam_policy" "raw_west" {
  binding {
    role    = "roles/storage.admin"
    members = [var.collector_impersonator_member]
  }
  binding {
    role    = "roles/storage.objectViewer"
    members = ["serviceAccount:${google_service_account.rebuild_operator.email}"]
  }
}
resource "google_storage_bucket_iam_policy" "raw_west" {
  bucket      = google_storage_bucket.raw_west.name
  policy_data = data.google_iam_policy.raw_west.policy_data
}
data "google_iam_policy" "preflight_west" {
  binding {
    role    = "roles/storage.admin"
    members = [var.collector_impersonator_member]
  }

}
resource "google_storage_bucket_iam_policy" "preflight_west" {
  bucket      = google_storage_bucket.preflight_west.name
  policy_data = data.google_iam_policy.preflight_west.policy_data
}

resource "google_cloud_run_v2_service" "fitbit_worker" {
  project             = var.project_id
  location            = var.west_region
  name                = "pdp-fitbit-worker-west"
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = { application = "personal-data-platform", role = "worker" }
  template {
    service_account                  = google_service_account.west.email
    timeout                          = "540s"
    max_instance_request_concurrency = 1
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = var.west_image_uri
      args  = ["fitbit", "serve-worker"]
      resources {
        limits   = { cpu = "1", memory = "2Gi" }
        cpu_idle = true
      }
      dynamic "env" {
        for_each = local.west_common_environment
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = { MOTHERDUCK_TOKEN = "motherduck_token", PDP_FITBIT_OAUTH_CONFIG = "fitbit_oauth_config" }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.west[env.value].secret_id
              version = var.west_secret_versions[env.value]
            }
          }
        }
      }
    }
  }
  depends_on = [google_service_account_iam_member.west_deployer, google_secret_manager_secret_iam_member.west_job]
}
resource "google_cloud_run_v2_service_iam_member" "fitbit_worker" {
  project  = var.project_id
  location = var.west_region
  name     = google_cloud_run_v2_service.fitbit_worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.west_scheduler.email}"
}
