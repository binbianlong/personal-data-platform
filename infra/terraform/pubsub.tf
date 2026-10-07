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
  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"
  retain_acked_messages      = false
  expiration_policy { ttl = "" }
  lifecycle { prevent_destroy = true }
}
resource "google_pubsub_topic_iam_member" "west_receiver" {
  project = var.project_id
  topic   = google_pubsub_topic.west.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.west_receiver.email}"
}
resource "google_pubsub_subscription_iam_member" "west_hourly" {
  project      = var.project_id
  subscription = google_pubsub_subscription.west.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.west.email}"
}
