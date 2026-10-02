locals {
  github_repository    = "${var.github_owner}/${var.github_repository}"
  plan_workflow_prefix = "${local.github_repository}/${var.plan_workflow_path}@"
  deploy_workflow_ref  = "${local.github_repository}/${var.deploy_workflow_path}@refs/heads/main"

  bootstrap_services = toset([
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "storage.googleapis.com",
  ])

  plan_project_roles = toset([
    "roles/viewer",
  ])

  deploy_project_roles = toset([
    "roles/cloudtasks.admin",
    "roles/cloudscheduler.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/logging.configWriter",
    "roles/monitoring.editor",
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/serviceusage.serviceUsageAdmin",
  ])
}

resource "google_project_service" "bootstrap" {
  for_each = local.bootstrap_services

  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

resource "google_storage_bucket" "terraform_state_us" {
  name                        = var.state_bucket_name
  project                     = var.project_id
  location                    = var.state_bucket_location
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.bootstrap]
}

# Retain the old bucket and its access bindings until runtime state has been
# migrated and verified. A region change must never replace the active backend.
removed {
  from = google_storage_bucket.terraform_state

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_storage_bucket_iam_member.github_plan_state

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_storage_bucket_iam_member.github_deploy_state

  lifecycle {
    destroy = false
  }
}

resource "google_artifact_registry_repository" "runtime_us" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repository_id
  description   = "Immutable runtime images for personal-data-platform"
  format        = "DOCKER"

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "delete-older-than-30-days"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "2592000s"
    }
  }

  cleanup_policies {
    id     = "keep-latest-five"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }

  cleanup_policies {
    id     = "keep-deployed"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["deployed-"]
    }
  }

  depends_on = [google_project_service.bootstrap]
}

# Keep a previously provisioned Asia repository and its images intact while
# creating the new us-central1 repository at a separate Terraform address.
removed {
  from = google_artifact_registry_repository.runtime

  lifecycle {
    destroy = false
  }
}
