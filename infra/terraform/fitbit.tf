variable "fitbit_subject_key" {
  description = "Stable pseudonymous owner key for Fitbit health data."
  type        = string
  default     = ""
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,128}$", var.fitbit_subject_key))
    error_message = "Fitbit ingestion requires a stable pseudonymous subject key."
  }
}
