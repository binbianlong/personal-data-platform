terraform {
  backend "gcs" {
    prefix = "personal-data-platform/runtime"
  }
}
