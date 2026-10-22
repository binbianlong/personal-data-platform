locals {
  runtime_secret_ids = {
    motherduck_token           = "pdp-west-motherduck-token"
    motherduck_preflight_token = "pdp-west-motherduck-preflight-token"
    fitbit_oauth_config        = "pdp-west-fitbit-oauth-config"
    fitbit_webhook_config      = "pdp-west-fitbit-webhook-config"
    heartbeat_config           = "pdp-west-heartbeat-config"
    owntracks_config           = "pdp-west-owntracks-config"
  }
}
# Secret versions are provisioned outside Terraform so payloads never enter state.
resource "google_secret_manager_secret" "runtime" {
  for_each  = local.runtime_secret_ids
  project   = var.project_id
  secret_id = each.value
  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.runtime]
}
