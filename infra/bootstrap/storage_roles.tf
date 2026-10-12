resource "google_project_iam_custom_role" "runtime_bucket_manager" {
  project     = var.project_id
  role_id     = "pdpRuntimeBucketManager"
  title       = "PDP runtime bucket manager"
  description = "Manages runtime bucket metadata and IAM without object data access."
  permissions = [
    "storage.buckets.create",
    "storage.buckets.delete",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
    "storage.buckets.list",
    "storage.buckets.setIamPolicy",
    "storage.buckets.update",
  ]
  stage = "GA"

  depends_on = [google_project_service.bootstrap]
}

resource "google_project_iam_custom_role" "runtime_bucket_reader" {
  project     = var.project_id
  role_id     = "pdpRuntimeBucketReader"
  title       = "PDP runtime bucket reader"
  description = "Reads runtime bucket metadata and IAM for Terraform planning without object access."
  permissions = [
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
  ]
  stage = "GA"

  depends_on = [google_project_service.bootstrap]
}
