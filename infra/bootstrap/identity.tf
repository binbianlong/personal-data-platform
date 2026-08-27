resource "google_iam_workload_identity_pool" "github_plan" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions-plan"
  display_name              = "GitHub Terraform plan"
  description               = "Read-only Terraform planning from the configured repository"

  depends_on = [google_project_service.bootstrap]
}

resource "google_iam_workload_identity_pool_provider" "github_plan" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github_plan.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub Terraform plan"

  attribute_mapping = {
    "google.subject"         = "assertion.sub"
    "attribute.actor"        = "assertion.actor"
    "attribute.event_name"   = "assertion.event_name"
    "attribute.ref"          = "assertion.ref"
    "attribute.repository"   = "assertion.repository"
    "attribute.workflow_ref" = "assertion.workflow_ref"
  }
  attribute_condition = join(" && ", [
    "assertion.repository == '${local.github_repository}'",
    "assertion.workflow_ref.startsWith('${local.plan_workflow_prefix}')",
    "assertion.event_name in ['pull_request', 'workflow_dispatch']",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_iam_workload_identity_pool" "github_deploy" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions-deploy"
  display_name              = "GitHub Terraform deploy"
  description               = "Production deployment from the approved main-branch workflow"

  depends_on = [google_project_service.bootstrap]
}

resource "google_iam_workload_identity_pool_provider" "github_deploy" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github_deploy.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub Terraform deploy"

  attribute_mapping = {
    "google.subject"         = "assertion.sub"
    "attribute.actor"        = "assertion.actor"
    "attribute.ref"          = "assertion.ref"
    "attribute.repository"   = "assertion.repository"
    "attribute.workflow_ref" = "assertion.workflow_ref"
  }
  attribute_condition = join(" && ", [
    "assertion.repository == '${local.github_repository}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.workflow_ref == '${local.deploy_workflow_ref}'",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "github_plan" {
  project      = var.project_id
  account_id   = "github-tf-plan"
  display_name = "GitHub Terraform plan"
  description  = "Read-only identity used only by terraform-plan.yml"
}

resource "google_service_account" "github_deploy" {
  project      = var.project_id
  account_id   = "github-tf-deploy"
  display_name = "GitHub Terraform deploy"
  description  = "Deployment identity used only by terraform-deploy.yml on main"
}

resource "google_service_account_iam_member" "github_plan_wif" {
  service_account_id = google_service_account.github_plan.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_plan.name}/attribute.repository/${local.github_repository}"
}

resource "google_service_account_iam_member" "github_deploy_wif" {
  service_account_id = google_service_account.github_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_deploy.name}/attribute.repository/${local.github_repository}"
}

resource "google_project_iam_member" "github_plan" {
  for_each = local.plan_project_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.github_plan.email}"
}

resource "google_project_iam_member" "github_deploy" {
  for_each = local.deploy_project_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.github_deploy.email}"
}

resource "google_storage_bucket_iam_member" "github_plan_state" {
  bucket = google_storage_bucket.terraform_state.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.github_plan.email}"
}

resource "google_storage_bucket_iam_member" "github_deploy_state" {
  bucket = google_storage_bucket.terraform_state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.github_deploy.email}"
}

resource "google_artifact_registry_repository_iam_member" "github_deploy_writer" {
  project    = google_artifact_registry_repository.runtime.project
  location   = google_artifact_registry_repository.runtime.location
  repository = google_artifact_registry_repository.runtime.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_deploy.email}"
}
