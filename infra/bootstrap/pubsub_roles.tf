resource "google_project_iam_custom_role" "runtime_pubsub_iam_reader" {
  project     = var.project_id
  role_id     = "pdpRuntimePubsubIamReader"
  title       = "PDP runtime Pub/Sub IAM reader"
  description = "Reads Pub/Sub IAM policies for Terraform planning."
  permissions = [
    "pubsub.subscriptions.getIamPolicy",
    "pubsub.topics.getIamPolicy",
  ]
}

resource "google_project_iam_member" "github_plan_pubsub_iam_reader" {
  project = var.project_id
  role    = google_project_iam_custom_role.runtime_pubsub_iam_reader.name
  member  = "serviceAccount:${google_service_account.github_plan.email}"
}
