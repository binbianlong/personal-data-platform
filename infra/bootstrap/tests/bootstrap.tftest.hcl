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
    condition     = google_storage_bucket.terraform_state.location == "ASIA"
    error_message = "The existing Terraform state location contract must remain ASIA by default."
  }

  assert {
    condition     = google_artifact_registry_repository.runtime_us.format == "DOCKER" && google_artifact_registry_repository.runtime_us.location == "us-central1"
    error_message = "The runtime registry must be a Docker repository."
  }

  assert {
    condition = alltrue([
      toset(google_project_iam_custom_role.collector_raw_creator.permissions) == toset(["storage.objects.create"]),
      toset(google_project_iam_custom_role.collector_receipt_writer.permissions) == toset(["storage.objects.create", "storage.objects.delete"]),
      toset(google_project_iam_custom_role.preflight_object_operator.permissions) == toset(["storage.objects.create", "storage.objects.delete", "storage.objects.get", "storage.objects.list"]),
    ])
    error_message = "Runtime object roles must contain only their required data-plane permissions."
  }

  assert {
    condition = alltrue([
      contains(google_project_iam_custom_role.runtime_bucket_manager.permissions, "storage.buckets.setIamPolicy"),
      !contains(google_project_iam_custom_role.runtime_bucket_manager.permissions, "storage.objects.get"),
      toset(google_project_iam_custom_role.runtime_bucket_reader.permissions) == toset(["storage.buckets.get", "storage.buckets.getIamPolicy"]),
    ])
    error_message = "Deploy and plan identities must receive only the required bucket control-plane permissions."
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
