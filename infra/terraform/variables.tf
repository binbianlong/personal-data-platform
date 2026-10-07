variable "project_id" {
  description = "Google Cloud project that hosts the runtime."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid Google Cloud project ID."
  }
}

variable "region" {
  description = "Region of retained legacy storage; normal compute uses west_region."
  type        = string
  default     = "us-central1"

  validation {
    condition     = var.region == "us-central1"
    error_message = "Legacy storage stays in us-central1 to avoid replacing retained backups."
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

variable "alert_email" {
  description = "Email address registered as a Cloud Monitoring notification channel."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[^[:space:]@]+@[^[:space:]@]+\\.[^[:space:]@]+$", var.alert_email))
    error_message = "alert_email must be a valid email address."
  }
}

variable "scheduler_time_zone" {
  description = "IANA time zone used by Cloud Scheduler."
  type        = string
  default     = "Asia/Tokyo"
}

variable "west_region" {
  description = "Region for normal compute, Raw and Pub/Sub."
  type        = string
  default     = "us-west1"
  validation {
    condition     = var.west_region == "us-west1"
    error_message = "West resources must use us-west1."
  }
}
variable "west_image_uri" {
  description = "Immutable image digest in the us-west1 Artifact Registry."
  type        = string
  default     = ""
  validation {
    condition     = can(regex("^us-west1-docker\\.pkg\\.dev/${var.project_id}/[^/]+/[^@]+@sha256:[0-9a-f]{64}$", var.west_image_uri))
    error_message = "Runtime requires its own digest in the us-west1 repository."
  }
}
variable "west_secret_versions" {
  description = "Non-secret numeric version pins for the west secret containers; secret payloads are provisioned outside Terraform."
  type        = map(string)
  default     = {}
  nullable    = false
  validation {
    condition     = alltrue([for key, version in var.west_secret_versions : contains(keys(local.west_secret_ids), key) && can(regex("^[1-9][0-9]*$", version))])
    error_message = "west_secret_versions must use existing west secret keys and positive numeric version strings; aliases such as latest are not allowed."
  }
  validation {
    condition     = length(setsubtract(toset(["motherduck_token", "fitbit_oauth_config", "fitbit_webhook_config", "heartbeat_config"]), toset(keys(var.west_secret_versions)))) == 0
    error_message = "Runtime requires version pins for motherduck_token, fitbit_oauth_config, fitbit_webhook_config, and heartbeat_config."
  }
}
variable "west_schedulers_enabled" {
  description = "Run scheduled processing and its native alerts; false pauses both schedules and Fitbit processing."
  type        = bool
  default     = true
}
variable "west_logging_exclusions" {
  description = "Existing _Default sink exclusions to preserve when routing ordinary logs west."
  type        = map(string)
  default     = {}
}
variable "west_motherduck_database" {
  description = "Production database in the separate us-west-2 MotherDuck organization."
  type        = string
  default     = "personal_data_platform_west"
}
variable "west_preflight_motherduck_database" {
  description = "Isolated preflight database in the west organization."
  type        = string
  default     = "personal_data_platform_west_preflight"
  validation {
    condition     = var.west_preflight_motherduck_database != var.west_motherduck_database
    error_message = "West preflight must use a separate database."
  }
}
