variable "project_id" {
  description = "Google Cloud project that hosts the runtime."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid Google Cloud project ID."
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

variable "rebuild_impersonator_member" {
  description = "Google user or group allowed to impersonate the dedicated Raw rebuild Service Account."
  type        = string

  validation {
    condition     = can(regex("^(user|group):[^[:space:]@]+@[^[:space:]@]+\\.[^[:space:]@]+$", var.rebuild_impersonator_member))
    error_message = "rebuild_impersonator_member must be a user: or group: IAM principal with an email address."
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

variable "region" {
  description = "Region for normal compute, Raw and Pub/Sub."
  type        = string
  default     = "us-west1"
  validation {
    condition     = var.region == "us-west1"
    error_message = "Runtime resources must use us-west1."
  }
}
variable "runtime_image_uri" {
  description = "Immutable image digest in the us-west1 Artifact Registry."
  type        = string
  default     = ""
  validation {
    condition     = can(regex("^us-west1-docker\\.pkg\\.dev/${var.project_id}/[^/]+/[^@]+@sha256:[0-9a-f]{64}$", var.runtime_image_uri))
    error_message = "Runtime requires its own digest in the us-west1 repository."
  }
}
variable "secret_versions" {
  description = "Non-secret numeric version pins for the runtime secret containers; secret payloads are provisioned outside Terraform."
  type        = map(string)
  default     = {}
  nullable    = false
  validation {
    condition     = alltrue([for key, version in var.secret_versions : contains(keys(local.runtime_secret_ids), key) && can(regex("^[1-9][0-9]*$", version))])
    error_message = "secret_versions must use existing runtime secret keys and positive numeric version strings; aliases such as latest are not allowed."
  }
  validation {
    condition     = length(setsubtract(toset(["motherduck_token", "fitbit_oauth_config", "fitbit_webhook_config", "heartbeat_config", "owntracks_config"]), toset(keys(var.secret_versions)))) == 0
    error_message = "Runtime requires version pins for motherduck_token, fitbit_oauth_config, fitbit_webhook_config, heartbeat_config, and owntracks_config."
  }
}
variable "schedulers_enabled" {
  description = "Run daily and Fitbit batch scheduling, notification processing and native alerts; false pauses schedules and processing."
  type        = bool
  default     = true
}
variable "logging_exclusions" {
  description = "Existing _Default sink exclusions to preserve when routing ordinary logs to the runtime bucket."
  type        = map(string)
  default     = {}
}
variable "motherduck_database" {
  description = "Production database in the us-west-2 MotherDuck organization."
  type        = string
  default     = "personal_data_platform"
}
variable "preflight_motherduck_database" {
  description = "Isolated preflight database in the production organization."
  type        = string
  default     = "personal_data_platform_west_preflight"
  validation {
    condition     = var.preflight_motherduck_database != var.motherduck_database
    error_message = "Preflight must use a separate database."
  }
}
