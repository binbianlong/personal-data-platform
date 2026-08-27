output "required_check_context" {
  description = "Status check required before updating the default branch."
  value       = local.required_check_context
}

output "ruleset_id" {
  description = "GitHub repository ruleset ID."
  value       = github_repository_ruleset.main.id
}
