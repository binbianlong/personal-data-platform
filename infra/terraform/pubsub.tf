resource "google_pubsub_topic" "west" {
  project = var.project_id
  name    = "pdp-fitbit-west"
  message_storage_policy {
    allowed_persistence_regions = [var.west_region]
    enforce_in_transit          = true
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.runtime]
}
resource "google_pubsub_subscription" "west" {
  project                    = var.project_id
  name                       = "pdp-fitbit-west-pull"
  topic                      = google_pubsub_topic.west.id
  ack_deadline_seconds       = 600
  message_retention_duration = "604800s"
  retain_acked_messages      = false
  expiration_policy { ttl = "" }
  dynamic "push_config" {
    for_each = var.west_schedulers_enabled ? [true] : []
    content {
      push_endpoint = "${google_cloud_run_v2_service.fitbit_worker.uri}/notifications/fitbit"
      oidc_token {
        service_account_email = google_service_account.west_scheduler.email
        audience              = google_cloud_run_v2_service.fitbit_worker.uri
      }
    }
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  depends_on = [google_cloud_run_v2_service_iam_member.fitbit_worker, google_service_account_iam_member.pubsub_push_token]
  lifecycle { prevent_destroy = true }
}
resource "google_pubsub_topic_iam_member" "west_receiver" {
  project = var.project_id
  topic   = google_pubsub_topic.west.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.west_receiver.email}"
}

data "google_project" "runtime" {
  project_id = var.project_id
}
resource "google_service_account_iam_member" "pubsub_push_token" {
  service_account_id = google_service_account.west_scheduler.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.runtime.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
