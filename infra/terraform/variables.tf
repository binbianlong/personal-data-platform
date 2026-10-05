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
  description = "Cron schedule for twice-daily reconciliation, within the Cloud Monitoring absence window."
  type        = string
  default     = "30 4,16 * * *"
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

variable "enable_west_runtime" {
  description = "Provision the parallel west deployment after its isolated dependencies are ready."
  type        = bool
  default     = false
}
variable "west_region" {
  description = "Region for the parallel deployment; legacy region remains unchanged."
  type        = string
  default     = "us-west1"
  validation {
    condition     = var.west_region == "us-west1"
    error_message = "West resources must use us-west1."
  }
}
variable "west_image_uri" {
  description = "Separate west Artifact Registry digest; never falls back to the legacy image."
  type        = string
  default     = ""
  validation {
    condition     = !var.enable_west_runtime || can(regex("^us-west1-docker\\.pkg\\.dev/${var.project_id}/[^/]+/[^@]+@sha256:[0-9a-f]{64}$", var.west_image_uri))
    error_message = "Enabled west deployment requires its own digest in the us-west1 repository."
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
    condition     = !var.enable_west_runtime || length(setsubtract(toset(["motherduck_token", "fitbit_oauth_config", "fitbit_webhook_config", "heartbeat_config"]), toset(keys(var.west_secret_versions)))) == 0
    error_message = "Enabled west deployment requires version pins for motherduck_token, fitbit_oauth_config, fitbit_webhook_config, and heartbeat_config."
  }
}
variable "west_schedulers_enabled" {
  description = "Explicit cutover gate; all west schedules are paused during preparation."
  type        = bool
  default     = false
  validation {
    condition     = !var.west_schedulers_enabled || var.enable_west_runtime
    error_message = "West schedules cannot be enabled before the west deployment exists."
  }
}
variable "west_logging_enabled" {
  description = "Redirect the existing _Default sink to west after reviewing existing exclusions."
  type        = bool
  default     = false
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
