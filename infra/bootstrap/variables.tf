variable "project_id" {
  description = "Google Cloud project that hosts the platform."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid Google Cloud project ID."
  }
}

variable "region" {
  description = "Google Cloud region used for the Artifact Registry repository."
  type        = string
  default     = "asia-northeast1"
}

variable "state_bucket_name" {
  description = "Globally unique GCS bucket name for runtime Terraform state."
  type        = string

  validation {
    condition     = length(trimspace(var.state_bucket_name)) >= 3
    error_message = "state_bucket_name must contain at least three characters."
  }
}

variable "state_bucket_location" {
  description = "Location for the Terraform state bucket."
  type        = string
  default     = "ASIA"
}

variable "artifact_repository_id" {
  description = "Artifact Registry Docker repository ID."
  type        = string
  default     = "personal-data-platform"
}

variable "github_owner" {
  description = "GitHub repository owner allowed to use Workload Identity Federation."
  type        = string
  default     = "binbianlong"
}

variable "github_repository" {
  description = "GitHub repository allowed to use Workload Identity Federation."
  type        = string
  default     = "personal-data-platform"
}

variable "plan_workflow_path" {
  description = "Repository-relative workflow path that is allowed to plan runtime Terraform."
  type        = string
  default     = ".github/workflows/terraform-plan.yml"

  validation {
    condition = (
      startswith(var.plan_workflow_path, ".github/workflows/") &&
      endswith(var.plan_workflow_path, ".yml")
    )
    error_message = "plan_workflow_path must identify a .yml file under .github/workflows/."
  }
}

variable "deploy_workflow_path" {
  description = "Repository-relative workflow path that is allowed to deploy from main."
  type        = string
  default     = ".github/workflows/terraform-deploy.yml"

  validation {
    condition = (
      startswith(var.deploy_workflow_path, ".github/workflows/") &&
      endswith(var.deploy_workflow_path, ".yml")
    )
    error_message = "deploy_workflow_path must identify a .yml file under .github/workflows/."
  }
}
