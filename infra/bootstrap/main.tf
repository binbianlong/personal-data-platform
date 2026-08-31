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
    "roles/iam.securityReviewer",
    "roles/viewer",
  ])

  deploy_project_roles = toset([
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

resource "google_storage_bucket" "terraform_state" {
  name                        = var.state_bucket_name
  project                     = var.project_id
  location                    = var.state_bucket_location
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.bootstrap]
}

resource "google_artifact_registry_repository" "runtime" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repository_id
  description   = "Immutable runtime images for personal-data-platform"
  format        = "DOCKER"

  depends_on = [google_project_service.bootstrap]
}
