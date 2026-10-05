locals {
  west_raw_bucket_name       = "${var.project_id}-pdp-raw-west"
  west_preflight_bucket_name = "${var.project_id}-pdp-preflight-west"
  west_common_environment = {
    APP_ENV                        = "production"
    GOOGLE_CLOUD_PROJECT           = var.project_id
    GCS_BUCKET                     = local.west_raw_bucket_name
    MOTHERDUCK_DATABASE            = var.west_motherduck_database
    PDP_SCHEMA_PROFILE             = "west"
    PDP_FITBIT_DELIVERY_MODE       = "pubsub"
    PDP_FITBIT_SUBJECT_KEY         = var.fitbit_subject_key
    PDP_FITBIT_PROCESSING_PAUSED   = tostring(!var.west_schedulers_enabled)
    PDP_FITBIT_PUBSUB_ENDPOINT     = "pubsub.us-west1.rep.googleapis.com"
    PDP_FITBIT_PUBSUB_TOPIC        = "projects/${var.project_id}/topics/pdp-fitbit-west"
    PDP_FITBIT_PUBSUB_SUBSCRIPTION = "projects/${var.project_id}/subscriptions/pdp-fitbit-west-pull"
    PDP_RAW_RETENTION_DAYS         = "90"
    PDP_LIFECYCLE_GRACE_DAYS       = "3"
  }
  west_jobs = var.enable_west_runtime ? {
    hourly = {
      name        = "fitbit-hourly-west"
      args        = ["fitbit", "ingest-notifications"]
      timeout     = "3000s"
      environment = local.west_common_environment
      secrets     = { MOTHERDUCK_TOKEN = "motherduck_token", PDP_FITBIT_OAUTH_CONFIG = "fitbit_oauth_config" }
    }
    daily = {
      name        = "reconciliation-west"
      args        = ["reconciliation", "--source", "screen_time", "--all-streams"]
      timeout     = "6000s"
      environment = merge(local.west_common_environment, { PDP_FITBIT_REPAIR_ENABLED = "true", PDP_RECONCILIATION_MONITORING_MODE = "healthchecks" })
      secrets     = { MOTHERDUCK_TOKEN = "motherduck_token", PDP_FITBIT_OAUTH_CONFIG = "fitbit_oauth_config", PDP_HEARTBEAT_CONFIG = "heartbeat_config" }
    }
    preflight = {
      name        = "platform-preflight-west"
      args        = ["preflight"]
      timeout     = "600s"
      environment = { APP_ENV = "production", GOOGLE_CLOUD_PROJECT = var.project_id, PDP_SCHEMA_PROFILE = "west", GCS_BUCKET = local.west_raw_bucket_name, GCS_PREFLIGHT_BUCKET = local.west_preflight_bucket_name, MOTHERDUCK_DATABASE = var.west_motherduck_database, PREFLIGHT_MOTHERDUCK_DATABASE = var.west_preflight_motherduck_database }
      secrets     = { MOTHERDUCK_TOKEN = "motherduck_preflight_token" }
    }
    dbt = {
      name        = "dbt-runner-west"
      args        = ["dbt"]
      timeout     = "6000s"
      environment = { APP_ENV = "production", PDP_SCHEMA_PROFILE = "west", MOTHERDUCK_DATABASE = var.west_motherduck_database }
      secrets     = { MOTHERDUCK_TOKEN = "motherduck_token" }
    }
  } : {}
  west_job_secret_access = merge([for job_key, job in local.west_jobs : {
    for secret_key in toset(values(job.secrets)) : "${job_key}:${secret_key}" => { job_key = job_key, secret_key = secret_key }
  }]...)
  west_scheduled_jobs = var.enable_west_runtime ? { hourly = "15 * * * *", daily = "30 4 * * *" } : {}
  west_raw_scopes     = { screen_time = { prefixes = ["raw/screen_time/v1/", "raw/screen_time/v2/"], suffixes = [".segb.gz"] }, fitbit = { prefixes = ["raw/fitbit/v2/"], suffixes = [".json.gz"] } }
}

resource "google_storage_bucket" "raw_west" {
  count                       = var.enable_west_runtime ? 1 : 0
  name                        = local.west_raw_bucket_name
  project                     = var.project_id
  location                    = var.west_region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  dynamic "lifecycle_rule" {
    for_each = local.west_raw_scopes
    content {
      action { type = "Delete" }
      condition {
        age            = 90
        matches_prefix = lifecycle_rule.value.prefixes
        matches_suffix = lifecycle_rule.value.suffixes
      }
    }
  }
  lifecycle_rule {
    action { type = "Delete" }
    condition {
      days_since_custom_time = 90
      matches_prefix         = ["raw/screen_time/v1/", "raw/screen_time/v2/"]
      matches_suffix         = [".segb.gz"]
    }
  }
  soft_delete_policy { retention_duration_seconds = 0 }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.runtime]
}
resource "google_storage_bucket" "preflight_west" {
  count                       = var.enable_west_runtime ? 1 : 0
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
  for_each     = local.west_jobs
  project      = var.project_id
  account_id   = each.value.name
  display_name = "PDP west ${each.key}"
  depends_on   = [google_project_service.runtime]
}
resource "google_service_account_iam_member" "west_deployer" {
  for_each           = local.west_jobs
  service_account_id = google_service_account.west[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}
resource "google_secret_manager_secret_iam_member" "west_job" {
  for_each  = local.west_job_secret_access
  project   = var.project_id
  secret_id = google_secret_manager_secret.west[each.value.secret_key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.west[each.value.job_key].email}"
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
      service_account = google_service_account.west[each.key].email
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
  depends_on = [google_service_account_iam_member.west_deployer, google_secret_manager_secret_iam_member.west_job, google_storage_bucket_iam_policy.raw_west, google_storage_bucket_iam_policy.preflight_west, google_pubsub_subscription_iam_member.west_hourly]
}
resource "google_service_account" "west_receiver" {
  count        = var.enable_west_runtime ? 1 : 0
  project      = var.project_id
  account_id   = "pdp-fitbit-receiver-west"
  display_name = "PDP west notification publisher"
  depends_on   = [google_project_service.runtime]
}
resource "google_service_account_iam_member" "west_receiver_deployer" {
  count              = var.enable_west_runtime ? 1 : 0
  service_account_id = google_service_account.west_receiver[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}
resource "google_secret_manager_secret_iam_member" "west_receiver" {
  count     = var.enable_west_runtime ? 1 : 0
  project   = var.project_id
  secret_id = google_secret_manager_secret.west["fitbit_webhook_config"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.west_receiver[0].email}"
}
resource "google_cloud_run_v2_service" "west" {
  count               = var.enable_west_runtime ? 1 : 0
  project             = var.project_id
  location            = var.west_region
  name                = "pdp-fitbit-west"
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = { application = "personal-data-platform", role = "receiver" }
  template {
    service_account                  = google_service_account.west_receiver[0].email
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
          PDP_FITBIT_DELIVERY_MODE   = "pubsub"
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
  count    = var.enable_west_runtime ? 1 : 0
  project  = var.project_id
  location = var.west_region
  name     = google_cloud_run_v2_service.west[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
resource "google_service_account" "west_scheduler" {
  count        = var.enable_west_runtime ? 1 : 0
  project      = var.project_id
  account_id   = "scheduler-invoker-west"
  display_name = "PDP west Scheduler invoker"
  depends_on   = [google_project_service.runtime]
}
resource "google_service_account_iam_member" "west_scheduler_deployer" {
  count              = var.enable_west_runtime ? 1 : 0
  service_account_id = google_service_account.west_scheduler[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}
resource "google_cloud_run_v2_job_iam_member" "west_scheduler" {
  for_each = local.west_scheduled_jobs
  project  = var.project_id
  location = var.west_region
  name     = google_cloud_run_v2_job.west[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.west_scheduler[0].email}"
}
resource "google_cloud_scheduler_job" "west" {
  for_each         = local.west_scheduled_jobs
  project          = var.project_id
  region           = var.west_region
  name             = "pdp-${each.key}-west"
  schedule         = each.value
  time_zone        = "Asia/Tokyo"
  paused           = !var.west_schedulers_enabled
  attempt_deadline = "320s"
  retry_config { retry_count = 0 }
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.west_region}/jobs/${google_cloud_run_v2_job.west[each.key].name}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = google_service_account.west_scheduler[0].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.west_scheduler, google_service_account_iam_member.west_scheduler_deployer]
}

data "google_iam_policy" "raw_west" {
  count = var.enable_west_runtime ? 1 : 0
  binding {
    role    = "roles/storage.admin"
    members = [var.collector_impersonator_member]
  }
  dynamic "binding" {
    for_each = { hourly = ["raw/fitbit/v2/"], daily = ["raw/fitbit/v2/", "raw/screen_time/v1/", "raw/screen_time/v2/"] }
    content {
      role    = "roles/storage.objectViewer"
      members = ["serviceAccount:${google_service_account.west[binding.key].email}"]
      condition {
        title      = "${binding.key}_raw_read"
        expression = "resource.name == 'projects/_/buckets/${local.west_raw_bucket_name}' || (${join(" || ", [for prefix in binding.value : "resource.name.startsWith('projects/_/buckets/${local.west_raw_bucket_name}/objects/${prefix}')"])})"
      }
    }
  }
  binding {
    role    = local.storage_roles.collector_raw_creator
    members = [for key in ["hourly", "daily"] : "serviceAccount:${google_service_account.west[key].email}"]
    condition {
      title      = "fitbit_bundle_create_only"
      expression = "resource.name.startsWith('projects/_/buckets/${local.west_raw_bucket_name}/objects/raw/fitbit/v2/') && resource.name.endsWith('.json.gz')"
    }
  }
  binding {
    role    = local.storage_roles.collector_raw_creator
    members = ["serviceAccount:${google_service_account.collector.email}"]
    condition {
      title      = "collector_raw_create_only"
      expression = "(resource.name.startsWith('projects/_/buckets/${local.west_raw_bucket_name}/objects/raw/screen_time/v1/') || resource.name.startsWith('projects/_/buckets/${local.west_raw_bucket_name}/objects/raw/screen_time/v2/')) && resource.name.endsWith('.segb.gz')"
    }
  }
  binding {
    role    = local.storage_roles.collector_receipt_writer
    members = ["serviceAccount:${google_service_account.collector.email}"]
    condition {
      title      = "collector_control_state_only"
      expression = "(resource.name.startsWith('projects/_/buckets/${local.west_raw_bucket_name}/objects/${local.receipt_object_prefix}') || resource.name == 'projects/_/buckets/${local.west_raw_bucket_name}/objects/${local.device_manifest_key}' || resource.name.startsWith('projects/_/buckets/${local.west_raw_bucket_name}/objects/${local.mac_receipt_object_prefix}') || resource.name == 'projects/_/buckets/${local.west_raw_bucket_name}/objects/${local.mac_device_manifest_key}') && resource.name.endsWith('.json')"
    }
  }
  binding {
    role    = "roles/storage.objectViewer"
    members = ["serviceAccount:${google_service_account.rebuild_operator.email}"]
  }
}
resource "google_storage_bucket_iam_policy" "raw_west" {
  count       = var.enable_west_runtime ? 1 : 0
  bucket      = google_storage_bucket.raw_west[0].name
  policy_data = data.google_iam_policy.raw_west[0].policy_data
}
data "google_iam_policy" "preflight_west" {
  count = var.enable_west_runtime ? 1 : 0
  binding {
    role    = "roles/storage.admin"
    members = [var.collector_impersonator_member]
  }
  binding {
    role    = local.storage_roles.preflight_object_operator
    members = ["serviceAccount:${google_service_account.west["preflight"].email}"]
    condition {
      title      = "isolated_preflight_objects"
      expression = "resource.name == 'projects/_/buckets/${local.west_preflight_bucket_name}' || resource.name.startsWith('projects/_/buckets/${local.west_preflight_bucket_name}/objects/test/preflight/')"
    }
  }
}
resource "google_storage_bucket_iam_policy" "preflight_west" {
  count       = var.enable_west_runtime ? 1 : 0
  bucket      = google_storage_bucket.preflight_west[0].name
  policy_data = data.google_iam_policy.preflight_west[0].policy_data
}
