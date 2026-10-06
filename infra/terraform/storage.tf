locals {
  raw_bucket_name           = "${var.project_id}-pdp-raw"
  preflight_bucket_name     = "${var.project_id}-pdp-preflight"
  raw_object_prefix         = "raw/screen_time/v1/"
  receipt_object_prefix     = "raw/screen_time/v1/_control/collector/latest/"
  device_manifest_key       = "raw/screen_time/v1/_control/collector/active.json"
  mac_receipt_object_prefix = "raw/screen_time/v1/_control/collector/app-usage/latest/"
  mac_device_manifest_key   = "raw/screen_time/v1/_control/collector/app-usage/active.json"

  storage_roles = {
    collector_raw_creator     = "projects/${var.project_id}/roles/pdpCollectorRawCreator"
    collector_receipt_writer  = "projects/${var.project_id}/roles/pdpCollectorReceiptWriter"
    preflight_object_operator = "projects/${var.project_id}/roles/pdpPreflightObjectOperator"
  }
}

resource "google_storage_bucket" "raw" {
  name                        = local.raw_bucket_name
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    action { type = "Delete" }
    condition {
      age            = 90
      matches_prefix = ["raw/screen_time/v1/", "raw/screen_time/v2/"]
      matches_suffix = [".segb.gz"]
    }
  }

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle { prevent_destroy = true }

  depends_on = [google_project_service.runtime]
}

resource "google_storage_bucket" "preflight" {
  name                        = local.preflight_bucket_name
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    action {
      type = "Delete"
    }

    condition {
      age            = 1
      matches_prefix = ["test/preflight/"]
    }
  }

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  depends_on = [google_project_service.runtime]
}

data "google_iam_policy" "raw_bucket" {
  binding {
    role    = "roles/storage.admin"
    members = [var.collector_impersonator_member]
  }

  binding {
    role    = "roles/storage.objectViewer"
    members = ["serviceAccount:${google_service_account.rebuild_operator.email}"]
  }
}

resource "google_storage_bucket_iam_policy" "raw" {
  bucket      = google_storage_bucket.raw.name
  policy_data = data.google_iam_policy.raw_bucket.policy_data
}

data "google_iam_policy" "preflight_bucket" {
  binding {
    role    = "roles/storage.admin"
    members = [var.collector_impersonator_member]
  }


}

resource "google_storage_bucket_iam_policy" "preflight" {
  bucket      = google_storage_bucket.preflight.name
  policy_data = data.google_iam_policy.preflight_bucket.policy_data
}
