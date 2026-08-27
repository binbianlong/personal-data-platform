output "state_bucket_name" {
  description = "GCS bucket passed to the runtime backend configuration."
  value       = google_storage_bucket.terraform_state.name
}

output "artifact_repository" {
  description = "Artifact Registry repository resource name."
  value       = google_artifact_registry_repository.runtime.name
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
