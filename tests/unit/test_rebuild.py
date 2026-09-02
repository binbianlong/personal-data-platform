from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_data_platform.recovery.rebuild import (
    rebuild_inventory,
    require_empty_rebuild_target,
    run_rebuild,
    run_rebuild_from_env,
    validate_rebuild_target,
)
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


def _configure_rebuild_adc(tmp_path: Path, monkeypatch) -> Path:
    service_account = "raw-rebuild-operator@synthetic-project.iam.gserviceaccount.com"
    adc_path = tmp_path / "gcloud-rebuild/application_default_credentials.json"
    adc_path.parent.mkdir(parents=True)
    adc_path.write_text(
        json.dumps(
            {
                "type": "impersonated_service_account",
                "service_account_impersonation_url": (
                    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                    f"{service_account}:generateAccessToken"
                ),
                "source_credentials": {"type": "authorized_user"},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(adc_path, 0o600)
    monkeypatch.setenv("PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS", str(adc_path))
    monkeypatch.setenv("PDP_REBUILD_SERVICE_ACCOUNT_EMAIL", service_account)
    return adc_path


def test_rebuild_inventory_is_order_independent() -> None:
    first = datetime(2026, 8, 26, tzinfo=UTC)
    observations = [
        SimpleNamespace(
            key="later",
            device_key="a",
            stream="app-in-focus",
            segment_key="one",
            observed_at=first + timedelta(days=1),
            storage_created_at=first + timedelta(days=2),
        ),
        SimpleNamespace(
            key="first",
            device_key="a",
            stream="app-in-focus",
            segment_key="one",
            observed_at=first,
            storage_created_at=first + timedelta(hours=1),
        ),
        SimpleNamespace(
            key="second-device",
            device_key="b",
            stream="app-in-focus",
            segment_key="two",
            observed_at=first,
            storage_created_at=first + timedelta(hours=2),
        ),
    ]

    inventory = rebuild_inventory(observations)

    assert inventory["raw_object_count"] == 3
    assert inventory["device_count"] == 2
    assert inventory["segment_count"] == 2
    assert inventory["first_observed_at"] == first.isoformat()
    assert inventory["first_storage_created_at"] == (first + timedelta(hours=1)).isoformat()
    assert inventory["last_storage_created_at"] == (first + timedelta(days=2)).isoformat()
    assert inventory["retention_days"] == 90
    assert inventory["full_history_rebuild_guaranteed"] is False


def test_rebuild_refuses_production_and_unsafe_names() -> None:
    with pytest.raises(ValueError, match="production"):
        validate_rebuild_target("personal_data", "personal_data")
    with pytest.raises(ValueError, match="only"):
        validate_rebuild_target("md:other?token=secret", "personal_data")

    validate_rebuild_target("personal-data-rebuild-20260827", "personal_data")


def test_rebuild_requires_an_empty_database(tmp_path) -> None:
    warehouse = Warehouse(connect(WarehouseConfig(str(tmp_path / "scratch.duckdb"))))
    try:
        require_empty_rebuild_target(warehouse)
        warehouse.connection.execute("CREATE TABLE existing (value INTEGER)")
        with pytest.raises(ValueError, match="existing"):
            require_empty_rebuild_target(warehouse)
    finally:
        warehouse.close()


def test_rebuild_ignores_tables_in_other_attached_databases() -> None:
    warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
    try:
        warehouse.connection.execute("ATTACH ':memory:' AS production")
        warehouse.connection.execute("CREATE TABLE production.main.existing (value INTEGER)")

        require_empty_rebuild_target(warehouse)

        warehouse.connection.execute("CREATE VIEW existing AS SELECT 1 AS value")
        with pytest.raises(ValueError, match="existing"):
            require_empty_rebuild_target(warehouse)
    finally:
        warehouse.close()


def test_rebuild_dry_run_uses_canonical_raw_prefix(tmp_path, monkeypatch) -> None:
    prefixes = []
    rebuild_adc = _configure_rebuild_adc(tmp_path, monkeypatch)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "ambient-collector-adc.json")

    class Repository:
        def list_raw(self, prefix):
            prefixes.append(prefix)
            return []

    monkeypatch.setenv("GCS_RAW_PREFIX", "test/not-screen-time/")

    def repository_from_env():
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(rebuild_adc.resolve())
        return Repository()

    monkeypatch.setattr(
        "personal_data_platform.storage.gcs.GCSRawRepository.from_env", repository_from_env
    )

    assert run_rebuild_from_env(dry_run=True, target_db=None) == 0
    assert prefixes == ["raw/screen_time/v1/"]
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "ambient-collector-adc.json"


def test_rebuild_rejects_adc_for_a_different_service_account(tmp_path, monkeypatch) -> None:
    _configure_rebuild_adc(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "PDP_REBUILD_SERVICE_ACCOUNT_EMAIL",
        "different@synthetic-project.iam.gserviceaccount.com",
    )

    with pytest.raises(ValueError, match="target does not match"):
        run_rebuild_from_env(dry_run=True, target_db=None)


@pytest.mark.parametrize("dbt_fails", [False, True])
def test_rebuild_uses_scratch_credentials_and_restores_environment(monkeypatch, dbt_fails) -> None:
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "ambient_database")
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "ambient_token")
    connections = []
    dbt_calls = []

    def connect_scratch(config):
        connections.append(config)
        return connect(WarehouseConfig(":memory:"))

    def transform(*, target):
        dbt_calls.append(target)
        assert os.environ["MOTHERDUCK_DATABASE"] == "scratch_database"
        assert os.environ["MOTHERDUCK_TOKEN"] == "scratch_token"
        if dbt_fails:
            raise RuntimeError("dbt failed")

    monkeypatch.setattr("personal_data_platform.recovery.rebuild.connect", connect_scratch)
    monkeypatch.setattr("personal_data_platform.recovery.rebuild.run_dbt", transform)

    def rebuild():
        return run_rebuild(
            SimpleNamespace(list_raw=lambda prefix: []),
            target_db="scratch_database",
            token="scratch_token",
            production_db="production_database",
            allow_partial_history=True,
        )

    if dbt_fails:
        with pytest.raises(RuntimeError, match="dbt failed"):
            rebuild()
    else:
        assert rebuild() == 0

    assert connections == [WarehouseConfig(database="scratch_database", token="scratch_token")]
    assert dbt_calls == ["prod"]
    assert os.environ["MOTHERDUCK_DATABASE"] == "ambient_database"
    assert os.environ["MOTHERDUCK_TOKEN"] == "ambient_token"


def test_rebuild_requires_explicit_partial_history_acknowledgement() -> None:
    with pytest.raises(ValueError, match="allow-partial-history"):
        run_rebuild(
            SimpleNamespace(list_raw=lambda prefix: []),
            target_db="scratch_database",
            token="scratch_token",
            production_db="production_database",
        )


def test_rebuild_uses_one_inventory_and_fails_if_a_listed_generation_disappears(
    monkeypatch,
) -> None:
    observed_at = datetime(2026, 8, 27, tzinfo=UTC)
    raw = SimpleNamespace(
        key="raw/disappeared.segb.gz",
        device_key="a" * 64,
        stream="app-in-focus",
        segment_key="b" * 64,
        observed_at=observed_at,
        sha256="c" * 64,
        storage_created_at=observed_at,
        storage_generation=9,
    )

    class Repository:
        def __init__(self) -> None:
            self.list_calls = 0

        def list_raw(self, prefix):
            self.list_calls += 1
            return [raw]

        def get_raw(self, key, *, generation):
            assert (key, generation) == (raw.key, 9)
            raise FileNotFoundError("lifecycle deleted the listed generation")

    repository = Repository()
    monkeypatch.setattr(
        "personal_data_platform.recovery.rebuild.connect",
        lambda config: connect(WarehouseConfig(":memory:")),
    )

    assert (
        run_rebuild(
            repository,  # type: ignore[arg-type]
            target_db="scratch_database",
            token="scratch_token",
            production_db="production_database",
            allow_partial_history=True,
        )
        == 1
    )
    assert repository.list_calls == 1
