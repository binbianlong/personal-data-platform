variable "project_id" {
  description = "Google Cloud project that hosts the runtime."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid Google Cloud project ID."
  }
}

variable "region" {
  description = "Google Cloud region for Cloud Run and Cloud Scheduler."
  type        = string
  default     = "us-central1"

  validation {
    condition     = var.region == "us-central1"
    error_message = "region is fixed to us-central1 for the runtime, Raw storage, and Artifact Registry."
  }
}

variable "deployer_service_account_email" {
  description = "Bootstrap-created deployment identity granted act-as only on runtime identities."
  type        = string

  validation {
    condition     = var.deployer_service_account_email == "github-tf-deploy@${var.project_id}.iam.gserviceaccount.com"
    error_message = "deployer_service_account_email must be the bootstrap-created github-tf-deploy account for project_id."
  }
}

variable "collector_impersonator_member" {
  description = "Google user or group allowed to impersonate the dedicated Mac Collector and rebuild Service Accounts."
  type        = string

  validation {
    condition     = can(regex("^(user|group):[^[:space:]@]+@[^[:space:]@]+\\.[^[:space:]@]+$", var.collector_impersonator_member))
    error_message = "collector_impersonator_member must be a user: or group: IAM principal with an email address."
  }
}

variable "image_uri" {
  description = "Immutable Artifact Registry image URI used by every Cloud Run Job."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image_uri))
    error_message = "image_uri must be immutable and end with @sha256:<64 lowercase hex characters>."
  }
}

variable "alert_email" {
  description = "Email address registered as a Cloud Monitoring notification channel."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[^[:space:]@]+@[^[:space:]@]+\\.[^[:space:]@]+$", var.alert_email))
    error_message = "alert_email must be a valid email address."
  }
}

variable "loader_schedule" {
  description = "Cron schedule for the hourly Screen Time loader."
  type        = string
  default     = "15 * * * *"
}

variable "reconciliation_schedule" {
  description = "Cron schedule for daily reconciliation."
  type        = string
  default     = "30 4 * * *"
}

variable "scheduler_time_zone" {
  description = "IANA time zone used by Cloud Scheduler."
  type        = string
  default     = "Asia/Tokyo"
}

variable "motherduck_database" {
  description = "Production MotherDuck database name."
  type        = string
  default     = "personal_data_platform"
}

variable "preflight_motherduck_database" {
  description = "Isolated MotherDuck database used by deployment preflight."
  type        = string
  default     = "personal_data_platform_test"
}
