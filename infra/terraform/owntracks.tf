locals {
  owntracks_services = {
    receiver = {
      name            = "pdp-owntracks-west"
      service_account = google_service_account.owntracks_receiver.email
      args            = ["owntracks", "serve"]
      timeout         = "60s"
      concurrency     = 16
      memory          = "512Mi"
      environment     = { GOOGLE_CLOUD_PROJECT = var.project_id, GCS_BUCKET = local.raw_bucket_name }
      secrets         = { PDP_OWNTRACKS_CONFIG = "owntracks_config" }
    }
    worker = {
      name            = "pdp-owntracks-worker-west"
      service_account = google_service_account.processing.email
      args            = ["owntracks", "serve-worker"]
      timeout         = "540s"
      concurrency     = 1
      memory          = "2Gi"
      environment = {
        GOOGLE_CLOUD_PROJECT              = var.project_id
        GCS_BUCKET                        = local.raw_bucket_name
        MOTHERDUCK_DATABASE               = var.motherduck_database
        PDP_OWNTRACKS_PUBSUB_SUBSCRIPTION = "projects/${var.project_id}/subscriptions/pdp-owntracks-west-push"
        PDP_OWNTRACKS_PROCESSING_PAUSED   = tostring(!var.schedulers_enabled)
      }
      secrets = { MOTHERDUCK_TOKEN = "motherduck_token" }
    }
  }
}

resource "google_service_account" "owntracks_receiver" {
  project      = var.project_id
  account_id   = "pdp-owntracks-receiver-west"
  display_name = "PDP OwnTracks Raw receiver"
  depends_on   = [google_project_service.runtime]
}
resource "google_service_account_iam_member" "owntracks_deployer" {
  service_account_id = google_service_account.owntracks_receiver.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account_email}"
}
resource "google_secret_manager_secret_iam_member" "owntracks_receiver" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.runtime["owntracks_config"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.owntracks_receiver.email}"
}
resource "google_cloud_run_v2_service" "owntracks" {
  for_each            = local.owntracks_services
  project             = var.project_id
  location            = var.region
  name                = each.value.name
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = { application = "personal-data-platform", role = each.key }
  template {
    service_account                  = each.value.service_account
    timeout                          = each.value.timeout
    max_instance_request_concurrency = each.value.concurrency
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = var.runtime_image_uri
      args  = each.value.args
      resources {
        limits   = { cpu = "1", memory = each.value.memory }
        cpu_idle = true
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
              version = var.secret_versions[env.value]
            }
          }
        }
      }
    }
  }
  depends_on = [google_service_account_iam_member.owntracks_deployer, google_secret_manager_secret_iam_member.owntracks_receiver, google_secret_manager_secret_iam_member.jobs, google_storage_bucket_iam_policy.raw]
}
resource "google_cloud_run_v2_service_iam_member" "owntracks_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.owntracks["receiver"].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
resource "google_cloud_run_v2_service_iam_member" "owntracks_worker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.owntracks["worker"].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}
resource "google_pubsub_topic" "owntracks" {
  project = var.project_id
  name    = "pdp-owntracks-west"
  message_storage_policy {
    allowed_persistence_regions = [var.region]
    enforce_in_transit          = true
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.runtime]
}
data "google_storage_project_service_account" "owntracks" {
  project = var.project_id
}
resource "google_pubsub_topic_iam_member" "owntracks_storage" {
  project = var.project_id
  topic   = google_pubsub_topic.owntracks.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${data.google_storage_project_service_account.owntracks.email_address}"
}
resource "google_storage_notification" "owntracks" {
  bucket             = google_storage_bucket.raw.name
  topic              = google_pubsub_topic.owntracks.id
  payload_format     = "JSON_API_V1"
  event_types        = ["OBJECT_FINALIZE"]
  object_name_prefix = "raw/owntracks/v1/"
  depends_on         = [google_pubsub_topic_iam_member.owntracks_storage]
}
resource "google_pubsub_subscription" "owntracks" {
  project                    = var.project_id
  name                       = "pdp-owntracks-west-push"
  topic                      = google_pubsub_topic.owntracks.id
  ack_deadline_seconds       = 600
  message_retention_duration = "604800s"
  retain_acked_messages      = false
  expiration_policy { ttl = "" }
  dynamic "push_config" {
    for_each = var.schedulers_enabled ? [true] : []
    content {
      push_endpoint = "${google_cloud_run_v2_service.owntracks["worker"].uri}/notifications/owntracks"
      oidc_token {
        service_account_email = google_service_account.scheduler.email
        audience              = google_cloud_run_v2_service.owntracks["worker"].uri
      }
    }
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  depends_on = [google_cloud_run_v2_service_iam_member.owntracks_worker, google_service_account_iam_member.pubsub_push_token]
  lifecycle { prevent_destroy = true }
}
