terraform {
  required_version = "~> 1.15.0"

  required_providers {
    motherduck = {
      source  = "motherduckdb/motherduck"
      version = "0.3.6"
    }
  }
}
