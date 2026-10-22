moved {
  from = google_storage_bucket.terraform_state
  to   = google_storage_bucket.terraform_state_backup
}

moved {
  from = google_storage_bucket.terraform_state_west
  to   = google_storage_bucket.runtime_state
}

moved {
  from = google_artifact_registry_repository.runtime_west
  to   = google_artifact_registry_repository.runtime
}

moved {
  from = google_storage_bucket_iam_member.github_plan_state
  to   = google_storage_bucket_iam_member.github_plan_state_backup
}

moved {
  from = google_storage_bucket_iam_member.github_deploy_state
  to   = google_storage_bucket_iam_member.github_deploy_state_backup
}

moved {
  from = google_storage_bucket_iam_member.github_plan_state_west
  to   = google_storage_bucket_iam_member.github_plan_runtime_state
}

moved {
  from = google_storage_bucket_iam_member.github_deploy_state_west
  to   = google_storage_bucket_iam_member.github_deploy_runtime_state
}

moved {
  from = google_artifact_registry_repository_iam_member.github_deploy_writer_west
  to   = google_artifact_registry_repository_iam_member.github_deploy_writer
}
