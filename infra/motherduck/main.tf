resource "motherduck_database" "production" {
  name                    = "personal_data_platform"
  database_type           = "default"
  transient               = true
  snapshot_retention_days = 0

  lifecycle {
    prevent_destroy = true
  }
}

resource "motherduck_share" "analytics" {
  name            = "pdp_analytics"
  source_database = motherduck_database.production.name
  access          = "restricted"
  visibility      = "discoverable"
  update_mode     = "automatic"

  lifecycle {
    prevent_destroy = true
  }
}

resource "motherduck_share_grant" "analytics_reader" {
  share        = motherduck_share.analytics.name
  username     = "hamaberyu"
  grantee_type = "user"
}
