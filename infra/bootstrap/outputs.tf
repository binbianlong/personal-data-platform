output "state_bucket_name" {
  description = "GCS bucket passed to the runtime backend configuration."
  value       = google_storage_bucket.terraform_state.name
}

output "artifact_repository" {
  description = "Artifact Registry repository resource name."
  value       = google_artifact_registry_repository.runtime_us.name
}

output "storage_custom_roles" {
  description = "Custom storage role names consumed by runtime Terraform."
  value = {
    collector_raw_creator     = google_project_iam_custom_role.collector_raw_creator.name
    collector_receipt_writer  = google_project_iam_custom_role.collector_receipt_writer.name
    preflight_object_operator = google_project_iam_custom_role.preflight_object_operator.name
    runtime_bucket_manager    = google_project_iam_custom_role.runtime_bucket_manager.name
    runtime_bucket_reader     = google_project_iam_custom_role.runtime_bucket_reader.name
  }
}

output "plan_workload_identity_provider" {
  description = "WIF provider used by the Terraform plan workflow."
  value       = google_iam_workload_identity_pool_provider.github_plan.name
}

output "plan_service_account" {
  description = "Service account impersonated by the Terraform plan workflow."
  value       = google_service_account.github_plan.email
}

output "deploy_workload_identity_provider" {
  description = "WIF provider used by the production deploy workflow."
  value       = google_iam_workload_identity_pool_provider.github_deploy.name
}

output "deploy_service_account" {
  description = "Service account impersonated by the production deploy workflow."
  value       = google_service_account.github_deploy.email
}

output "deploy_attribute_condition" {
  description = "Effective repository, branch, and workflow restriction for deployment."
  value       = google_iam_workload_identity_pool_provider.github_deploy.attribute_condition
}

output "plan_attribute_condition" {
  description = "Effective repository, workflow, and event restriction for planning."
  value       = google_iam_workload_identity_pool_provider.github_plan.attribute_condition
}
