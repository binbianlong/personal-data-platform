resource "google_storage_bucket" "profile" {
  name                        = "${var.project_id}-pdp-profile-west"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "inherited"
  soft_delete_policy { retention_duration_seconds = 0 }
  depends_on = [google_project_service.runtime]
}

resource "google_storage_bucket_iam_binding" "profile_reader" {
  bucket  = google_storage_bucket.profile.name
  role    = "roles/storage.objectViewer"
  members = ["allUsers"]
}

resource "google_storage_bucket_iam_binding" "profile_writer" {
  bucket  = google_storage_bucket.profile.name
  role    = "roles/storage.objectUser"
  members = ["serviceAccount:${google_service_account.processing.email}"]
}
