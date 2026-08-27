locals {
  runtime_secrets = {
    b2_key_id = {
      secret_id = "b2-key-id"
    }
    b2_application_key = {
      secret_id = "b2-application-key"
    }
    b2_endpoint = {
      secret_id = "b2-endpoint"
    }
    b2_bucket = {
      secret_id = "b2-bucket"
    }
    preflight_b2_key_id = {
      secret_id = "preflight-b2-key-id"
    }
    preflight_b2_application_key = {
      secret_id = "preflight-b2-application-key"
    }
    motherduck_token = {
      secret_id = "motherduck-token"
    }
    motherduck_preflight_token = {
      secret_id = "motherduck-preflight-token"
    }
    healthchecks_ping_url = {
      secret_id = "healthchecks-ping-url"
    }
  }
}

resource "google_secret_manager_secret" "runtime" {
  for_each = local.runtime_secrets

  project   = var.project_id
  secret_id = each.value.secret_id

  replication {
    auto {}
  }

  labels = {
    application = "personal-data-platform"
    managed_by  = "terraform"
  }

  depends_on = [google_project_service.runtime]
}
