resource "google_pubsub_topic" "fitbit" {
  project = var.project_id
  name    = "pdp-fitbit-west"
  message_storage_policy {
    allowed_persistence_regions = [var.region]
    enforce_in_transit          = true
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.runtime]
}
resource "google_pubsub_subscription" "fitbit" {
  project                    = var.project_id
  name                       = "pdp-fitbit-west-pull"
  topic                      = google_pubsub_topic.fitbit.id
  ack_deadline_seconds       = 600
  message_retention_duration = "604800s"
  retain_acked_messages      = false
  expiration_policy { ttl = "" }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  lifecycle { prevent_destroy = true }
}
resource "google_pubsub_subscription_iam_member" "fitbit_worker" {
  project      = var.project_id
  subscription = google_pubsub_subscription.fitbit.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.processing.email}"
}
resource "google_pubsub_topic_iam_member" "fitbit_receiver" {
  project = var.project_id
  topic   = google_pubsub_topic.fitbit.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.fitbit_receiver.email}"
}

data "google_project" "runtime" {
  project_id = var.project_id
}
resource "google_service_account_iam_member" "pubsub_push_token" {
  service_account_id = google_service_account.scheduler.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.runtime.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
