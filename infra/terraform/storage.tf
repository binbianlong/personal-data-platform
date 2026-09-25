locals {
  raw_bucket_name       = "${var.project_id}-pdp-raw"
  preflight_bucket_name = "${var.project_id}-pdp-preflight"
  raw_object_prefix     = "raw/screen_time/v1/"
  receipt_object_prefix = "raw/screen_time/v1/_control/collector/latest/"
  device_manifest_key   = "raw/screen_time/v1/_control/collector/active.json"

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

  dynamic "lifecycle_rule" {
    for_each = local.ingestion_pipelines
    content {
      action { type = "Delete" }
      condition {
        age            = lifecycle_rule.value.retention_days
        matches_prefix = lifecycle_rule.value.raw_prefixes
        matches_suffix = lifecycle_rule.value.raw_suffixes
      }
    }
  }

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = length(distinct([
        for pipeline in values(local.ingestion_pipelines) : "${pipeline.source_id}/${pipeline.stream}"
      ])) == length(local.ingestion_pipelines)
      error_message = "Each source and stream must have exactly one ingestion pipeline."
    }

    precondition {
      condition     = local.compatible_raw_retention
      error_message = "Overlapping Raw prefix and suffix scopes must use the same retention period."
    }
  }

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

  dynamic "binding" {
    for_each = { for key, pipeline in local.ingestion_pipelines : key => pipeline if length(pipeline.raw_creator_members) > 0 }
    content {
      role    = local.storage_roles.collector_raw_creator
      members = binding.value.raw_creator_members
      condition {
        title       = binding.key == "screen_time_app_in_focus" ? "collector_raw_create_only" : "${binding.key}_raw_create_only"
        description = "Allow immutable Raw creation only in the configured source namespace."
        expression  = "(${join(" || ", [for prefix in binding.value.raw_prefixes : "resource.name.startsWith('projects/_/buckets/${local.raw_bucket_name}/objects/${prefix}')"])}) && (${join(" || ", [for suffix in binding.value.raw_suffixes : "resource.name.endsWith('${suffix}')"])})"
      }
    }
  }

  binding {
    role    = local.storage_roles.collector_receipt_writer
    members = ["serviceAccount:${google_service_account.collector.email}"]

    condition {
      title       = "collector_control_state_only"
      description = "Allow replacement of latest receipts and the expected-device manifest only."
      expression  = "(resource.name.startsWith('projects/_/buckets/${local.raw_bucket_name}/objects/${local.receipt_object_prefix}') && resource.name.endsWith('.json')) || resource.name == 'projects/_/buckets/${local.raw_bucket_name}/objects/${local.device_manifest_key}'"
    }
  }

  binding {
    role = "roles/storage.objectViewer"
    members = concat(
      [for key in keys(local.ingestion_jobs) : "serviceAccount:${google_service_account.runtime[key].email}"],
      ["serviceAccount:${google_service_account.rebuild_operator.email}"]
    )
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

  binding {
    role    = local.storage_roles.preflight_object_operator
    members = ["serviceAccount:${google_service_account.runtime["preflight"].email}"]
  }
}

resource "google_storage_bucket_iam_policy" "preflight" {
  bucket      = google_storage_bucket.preflight.name
  policy_data = data.google_iam_policy.preflight_bucket.policy_data
}
