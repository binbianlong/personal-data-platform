mock_provider "motherduck" {}

run "production_storage_and_access" {
  command = plan

  assert {
    condition = (
      motherduck_database.production.name == "personal_data_platform" &&
      motherduck_database.production.database_type == "default" &&
      motherduck_database.production.transient &&
      motherduck_database.production.snapshot_retention_days == 0
    )
    error_message = "Production must use transient native storage without historical retention."
  }

  assert {
    condition = (
      motherduck_share.analytics.name == "pdp_analytics" &&
      motherduck_share.analytics.source_database == motherduck_database.production.name &&
      motherduck_share.analytics.access == "restricted" &&
      motherduck_share.analytics.visibility == "discoverable" &&
      motherduck_share.analytics.update_mode == "automatic" &&
      motherduck_share.analytics.include_pattern == null
    )
    error_message = "Analytics must expose the production database through a restricted, discoverable, automatic share."
  }

  assert {
    condition = (
      motherduck_share_grant.analytics_reader.share == motherduck_share.analytics.name &&
      motherduck_share_grant.analytics_reader.username == "hamaberyu" &&
      motherduck_share_grant.analytics_reader.grantee_type == "user"
    )
    error_message = "The analytics READ grant must belong to the hamaberyu user."
  }
}
