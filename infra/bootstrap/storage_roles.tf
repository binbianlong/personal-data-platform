resource "google_project_iam_custom_role" "collector_raw_creator" {
  project     = var.project_id
  role_id     = "pdpCollectorRawCreator"
  title       = "PDP Collector Raw creator"
  description = "Creates immutable Raw objects without read, list, overwrite, or delete access."
  permissions = ["storage.objects.create"]
  stage       = "GA"

  depends_on = [google_project_service.bootstrap]
}

resource "google_project_iam_custom_role" "collector_receipt_writer" {
  project     = var.project_id
  role_id     = "pdpCollectorReceiptWriter"
  title       = "PDP Collector receipt writer"
  description = "Creates and replaces only Collector control JSON selected by bucket IAM conditions."
  permissions = [
    "storage.objects.create",
    "storage.objects.delete",
  ]
  stage = "GA"

  depends_on = [google_project_service.bootstrap]
}

resource "google_project_iam_custom_role" "preflight_object_operator" {
  project     = var.project_id
  role_id     = "pdpPreflightObjectOperator"
  title       = "PDP preflight object operator"
  description = "Creates, reads, lists, and deletes objects in the isolated preflight bucket."
  permissions = [
    "storage.objects.create",
    "storage.objects.delete",
    "storage.objects.get",
    "storage.objects.list",
  ]
  stage = "GA"

  depends_on = [google_project_service.bootstrap]
}

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
