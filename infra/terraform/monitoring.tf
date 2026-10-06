resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "personal-data-platform operations email"
  type         = "email"

  labels = {
    email_address = var.alert_email
  }

  user_labels = {
    application = "personal-data-platform"
    managed_by  = "terraform"
  }

  depends_on = [google_project_service.runtime]
}
