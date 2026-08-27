variable "github_owner" {
  description = "Owner of the GitHub repository."
  type        = string
  default     = "binbianlong"

  validation {
    condition     = length(trimspace(var.github_owner)) > 0
    error_message = "github_owner must not be empty."
  }
}

variable "github_repository" {
  description = "GitHub repository name."
  type        = string
  default     = "personal-data-platform"

  validation {
    condition     = length(trimspace(var.github_repository)) > 0
    error_message = "github_repository must not be empty."
  }
}
