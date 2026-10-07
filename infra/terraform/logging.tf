resource "google_logging_project_bucket_config" "west" {
  project        = var.project_id
  location       = var.west_region
  bucket_id      = "pdp-west"
  retention_days = 30
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.runtime]
}
# The provider acquires the existing _Default sink, keeping one ordinary-log route.
# _Required and existing log buckets remain outside this configuration.
resource "google_logging_project_sink" "default_west" {
  project     = var.project_id
  name        = "_Default"
  destination = "logging.googleapis.com/${google_logging_project_bucket_config.west.id}"
  filter      = "NOT LOG_ID(\"cloudaudit.googleapis.com/activity\") AND NOT LOG_ID(\"cloudaudit.googleapis.com/system_event\") AND NOT LOG_ID(\"cloudaudit.googleapis.com/access_transparency\")"
  # _Default exposes its existing Google-managed identity on every API read.
  unique_writer_identity = true
  dynamic "exclusions" {
    for_each = var.west_logging_exclusions
    content {
      name   = exclusions.key
      filter = exclusions.value
    }
  }
}
