mock_provider "github" {}

run "default_repository_ruleset" {
  command = plan

  assert {
    condition     = github_repository_ruleset.main.repository == "personal-data-platform"
    error_message = "The ruleset must protect the personal-data-platform repository."
  }

  assert {
    condition     = github_repository_ruleset.main.enforcement == "active"
    error_message = "The ruleset must be active."
  }

  assert {
    condition     = one(github_repository_ruleset.main.conditions[0].ref_name[0].include) == "~DEFAULT_BRANCH"
    error_message = "The ruleset must target the default branch."
  }

  assert {
    condition     = github_repository_ruleset.main.rules[0].pull_request[0].required_approving_review_count == 0
    error_message = "Changes must use a pull request without adding an approval requirement."
  }

  assert {
    condition     = one(github_repository_ruleset.main.rules[0].required_status_checks[0].required_check).context == output.required_check_context
    error_message = "The ruleset must require the CI job."
  }

  assert {
    condition     = one(github_repository_ruleset.main.rules[0].required_status_checks[0].required_check).integration_id == 15368
    error_message = "The required check must come from GitHub Actions."
  }

  assert {
    condition     = github_repository_ruleset.main.rules[0].required_status_checks[0].strict_required_status_checks_policy
    error_message = "The CI check must run against the latest default branch."
  }
}
