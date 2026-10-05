locals {
  runtime_secrets = merge({
    motherduck_token = {
      secret_id = "motherduck-token"
    }
    motherduck_preflight_token = {
      secret_id = "motherduck-preflight-token"
    }
    healthchecks_ping_url = {
      secret_id = "healthchecks-ping-url"
    }
    }, {
    for key, pipeline in var.additional_ingestion_pipelines : "${key}_heartbeat" => {
      secret_id = "pdp-${replace(key, "_", "-")}-heartbeat"
    }
  })

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

# Terraform 1.15 cannot remove one for_each instance directly. Move each former
# B2 container to a temporary whole-resource address, then forget that address
# with destroy disabled. Existing secrets survive the cutover; a fresh project
# does not create empty legacy containers.
moved {
  from = google_secret_manager_secret.runtime["b2_key_id"]
  to   = google_secret_manager_secret.retained_b2_key_id
}

removed {
  from = google_secret_manager_secret.retained_b2_key_id

  lifecycle {
    destroy = false
  }
}

moved {
  from = google_secret_manager_secret.runtime["b2_application_key"]
  to   = google_secret_manager_secret.retained_b2_application_key
}

removed {
  from = google_secret_manager_secret.retained_b2_application_key

  lifecycle {
    destroy = false
  }
}

moved {
  from = google_secret_manager_secret.runtime["b2_endpoint"]
  to   = google_secret_manager_secret.retained_b2_endpoint
}

removed {
  from = google_secret_manager_secret.retained_b2_endpoint

  lifecycle {
    destroy = false
  }
}

moved {
  from = google_secret_manager_secret.runtime["b2_bucket"]
  to   = google_secret_manager_secret.retained_b2_bucket
}

removed {
  from = google_secret_manager_secret.retained_b2_bucket

  lifecycle {
    destroy = false
  }
}

moved {
  from = google_secret_manager_secret.runtime["preflight_b2_key_id"]
  to   = google_secret_manager_secret.retained_preflight_b2_key_id
}

removed {
  from = google_secret_manager_secret.retained_preflight_b2_key_id

  lifecycle {
    destroy = false
  }
}

moved {
  from = google_secret_manager_secret.runtime["preflight_b2_application_key"]
  to   = google_secret_manager_secret.retained_preflight_b2_application_key
}

removed {
  from = google_secret_manager_secret.retained_preflight_b2_application_key

  lifecycle {
    destroy = false
  }
}

locals {
  west_secret_ids = {
    motherduck_token           = "pdp-west-motherduck-token"
    motherduck_preflight_token = "pdp-west-motherduck-preflight-token"
    fitbit_oauth_config        = "pdp-west-fitbit-oauth-config"
    fitbit_webhook_config      = "pdp-west-fitbit-webhook-config"
    heartbeat_config           = "pdp-west-heartbeat-config"
  }
}
# Secret versions are provisioned outside Terraform so payloads never enter state.
resource "google_secret_manager_secret" "west" {
  for_each  = var.enable_west_runtime ? local.west_secret_ids : {}
  project   = var.project_id
  secret_id = each.value
  replication {
    user_managed {
      replicas { location = var.west_region }
    }
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.runtime]
}
