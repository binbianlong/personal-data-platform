mock_provider "google" {}

run "secure_bootstrap_contract" {
  command = plan

  variables {
    project_id        = "example-project"
    state_bucket_name = "example-project-personal-data-platform-tfstate"
  }

  assert {
    condition     = google_storage_bucket.terraform_state.uniform_bucket_level_access
    error_message = "Terraform state must use uniform bucket-level access."
  }

  assert {
    condition     = google_storage_bucket.terraform_state.public_access_prevention == "enforced"
    error_message = "Terraform state must prevent public access."
  }

  assert {
    condition     = google_storage_bucket.terraform_state.versioning[0].enabled
    error_message = "Terraform state versioning must be enabled."
  }

  assert {
    condition     = google_artifact_registry_repository.runtime.format == "DOCKER"
    error_message = "The runtime registry must be a Docker repository."
  }

  assert {
    condition     = google_service_account.github_plan.account_id != google_service_account.github_deploy.account_id
    error_message = "Terraform plan and deploy must use separate service accounts."
  }

  assert {
    condition = alltrue([
      strcontains(output.plan_attribute_condition, "assertion.repository == 'binbianlong/personal-data-platform'"),
      strcontains(output.plan_attribute_condition, "assertion.workflow_ref.startsWith('binbianlong/personal-data-platform/.github/workflows/terraform-plan.yml@')"),
      strcontains(output.plan_attribute_condition, "assertion.event_name in ['pull_request', 'workflow_dispatch']"),
    ])
    error_message = "Plan federation must be restricted by repository, workflow path, and event."
  }

  assert {
    condition = alltrue([
      strcontains(output.deploy_attribute_condition, "assertion.repository == 'binbianlong/personal-data-platform'"),
      strcontains(output.deploy_attribute_condition, "assertion.ref == 'refs/heads/main'"),
      strcontains(output.deploy_attribute_condition, "assertion.workflow_ref == 'binbianlong/personal-data-platform/.github/workflows/terraform-deploy.yml@refs/heads/main'"),
    ])
    error_message = "Deploy federation must be restricted by repository, main, and workflow path."
  }

  assert {
    condition     = google_storage_bucket_iam_member.github_plan_state.role == "roles/storage.objectViewer"
    error_message = "The plan identity must have read-only state access."
  }

  assert {
    condition     = google_storage_bucket_iam_member.github_deploy_state.role == "roles/storage.objectAdmin"
    error_message = "The deploy identity must be able to update and lock state."
  }

  assert {
    condition = alltrue([
      !contains(local.deploy_project_roles, "roles/resourcemanager.projectIamAdmin"),
      !contains(local.deploy_project_roles, "roles/iam.serviceAccountUser"),
    ])
    error_message = "Runtime deployment must not receive project-wide IAM or act-as permissions."
  }
}
