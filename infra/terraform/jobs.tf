resource "google_service_account" "rebuild_operator" {
  project      = var.project_id
  account_id   = "raw-rebuild-operator"
  display_name = "personal-data-platform Raw rebuild operator"
  description  = "Read-only identity impersonated for explicit local partial-history rebuilds"

  depends_on = [google_project_service.runtime]
}

resource "google_service_account_iam_member" "rebuild_operator_impersonator" {
  service_account_id = google_service_account.rebuild_operator.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = var.rebuild_impersonator_member
}
