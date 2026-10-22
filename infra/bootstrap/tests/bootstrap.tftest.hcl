mock_provider "google" {}

run "secure_bootstrap_contract" {
  command = plan
  override_resource {
    target          = google_artifact_registry_repository.runtime
    override_during = plan
    values          = { name = "projects/example-project/locations/us-west1/repositories/personal-data-platform" }
  }

  variables {
    project_id        = "example-project"
    state_bucket_name = "example-project-personal-data-platform-tfstate"
  }

  assert {
    condition     = output.artifact_repository == google_artifact_registry_repository.runtime.name && !contains(local.deploy_project_roles, "roles/cloudtasks.admin")
    error_message = "Normal deployment must use the runtime registry without Cloud Tasks administration."
  }

  assert {
    condition     = output.state_bucket_name == google_storage_bucket.runtime_state.name
    error_message = "The runtime backend output must point to the active runtime state bucket."
  }

  assert {
    condition     = google_storage_bucket.terraform_state_backup.uniform_bucket_level_access
    error_message = "Terraform state must use uniform bucket-level access."
  }

  assert {
    condition     = google_storage_bucket.terraform_state_backup.public_access_prevention == "enforced"
    error_message = "Terraform state must prevent public access."
  }

  assert {
    condition     = google_storage_bucket.terraform_state_backup.versioning[0].enabled
    error_message = "Terraform state versioning must be enabled."
  }

  assert {
    condition     = google_storage_bucket.terraform_state_backup.location == "ASIA"
    error_message = "The existing Terraform state location contract must remain ASIA by default."
  }

  assert {
    condition     = google_artifact_registry_repository.runtime.format == "DOCKER" && google_artifact_registry_repository.runtime.location == "us-west1"
    error_message = "The runtime registry must be a Docker repository."
  }

  assert {
    condition     = length(google_artifact_registry_repository.runtime.cleanup_policies) == 2
    error_message = "Cleanup must have only one expiration rule and one latest-version exception."
  }

  assert {
    condition = anytrue([
      for policy in google_artifact_registry_repository.runtime.cleanup_policies :
      policy.action == "KEEP" && try(policy.most_recent_versions[0].keep_count == 1, false)
    ])
    error_message = "Cleanup must retain exactly one most recent version of each image."
  }

  assert {
    condition = !google_artifact_registry_repository.runtime.cleanup_policy_dry_run && anytrue([
      for policy in google_artifact_registry_repository.runtime.cleanup_policies :
      policy.action == "DELETE" && try(
        policy.condition[0].tag_state == "ANY" &&
        policy.condition[0].older_than == "86400s", false
      )
    ])
    error_message = "Cleanup must expire old SHA-tagged images as well as untagged images after one day."
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
    condition     = google_storage_bucket_iam_member.github_plan_state_backup.role == "roles/storage.objectViewer"
    error_message = "The plan identity must have read-only state access."
  }

  assert {
    condition     = google_storage_bucket_iam_member.github_deploy_state_backup.role == "roles/storage.objectAdmin"
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

run "runtime_state_preserves_backup" {
  command = plan
  variables {
    project_id        = "example-project"
    state_bucket_name = "example-project-personal-data-platform-tfstate"
  }
  assert {
    condition     = google_storage_bucket.terraform_state_backup.location == "ASIA" && google_storage_bucket.runtime_state.location == "us-west1" && google_storage_bucket.terraform_state_backup.name != google_storage_bucket.runtime_state.name
    error_message = "Runtime state and retained backup must use distinct protected buckets."
  }
  assert {
    condition     = google_artifact_registry_repository.runtime.location == "us-west1" && contains(local.deploy_project_roles, "roles/pubsub.admin") && local.plan_project_roles == toset(["roles/viewer"])
    error_message = "Normal registry must be in us-west1 and only deploy may administer Pub/Sub."
  }
}
