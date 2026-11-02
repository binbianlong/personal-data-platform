from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_data_platform.raw.models import RawObject
from personal_data_platform.recovery.rebuild import (
    rebuild_inventory,
    require_empty_rebuild_target,
    run_rebuild,
    run_rebuild_from_env,
    validate_rebuild_target,
)
from personal_data_platform.sources.iphone_screen_time.raw import ScreenTimeRawIdentity
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect


def test_unlimited_gcs_rebuild_uses_read_only_adc_and_restores_environment(tmp_path, monkeypatch):
    from personal_data_platform.recovery import rebuild
    from personal_data_platform.sources.owntracks.adapter import OwnTracksSource

    path = tmp_path / "read-only-adc.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "previous-adc")
    monkeypatch.setattr(
        rebuild.RebuildADCConfig, "from_env", lambda: SimpleNamespace(credentials_path=path)
    )

    class EmptyRepository:
        def list_raw(self, prefix):
            assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(path)
            return []

    monkeypatch.setattr(OwnTracksSource, "repository_from_env", lambda _: EmptyRepository())
    assert run_rebuild_from_env(dry_run=True, target_db=None, source_id="owntracks") == 0
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "previous-adc"


def _raw(
    name: str, subject: str, observed_at: datetime, created_at: datetime, *, generation: int = 1
) -> RawObject:
    identity = ScreenTimeRawIdentity(
        device_key=hashlib.sha256(subject.encode()).hexdigest(),
        stream="app-in-focus",
        segment_key=hashlib.sha256(name.encode()).hexdigest(),
        observed_at=observed_at,
        sha256="c" * 64,
    )
    return get_source().parse_raw_key(
        identity.object_key, storage_created_at=created_at, storage_generation=generation
    )


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
        _raw("one", "a", first + timedelta(days=1), first + timedelta(days=2)),
        _raw("one", "a", first, first + timedelta(hours=1)),
        _raw("two", "b", first, first + timedelta(hours=2)),
    ]

    inventory = rebuild_inventory(observations)

    assert inventory["raw_object_count"] == 3
    assert inventory["device_count"] == 2
    assert inventory["segment_count"] == 2
    assert inventory["first_observed_at"] == first.isoformat()
    assert inventory["first_storage_created_at"] == (first + timedelta(hours=1)).isoformat()
    assert inventory["last_storage_created_at"] == (first + timedelta(days=2)).isoformat()
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
    _configure_rebuild_adc(tmp_path, monkeypatch)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "ambient-collector-adc.json")

    class Repository:
        def list_raw(self, prefix):
            prefixes.append(prefix)
            return []

    monkeypatch.setenv("GCS_RAW_PREFIX", "test/not-screen-time/")

    def repository_from_env(**_):
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "ambient-collector-adc.json"
        return Repository()

    monkeypatch.setattr(
        "personal_data_platform.sources.iphone_screen_time.storage.LocalRepository.from_env",
        repository_from_env,
    )

    assert run_rebuild_from_env(dry_run=True, target_db=None) == 0
    assert prefixes == ["raw/iphone_screen_time/v2/"]
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "ambient-collector-adc.json"


@pytest.mark.parametrize("dbt_fails", [False, True])
def test_rebuild_uses_scratch_credentials_and_restores_environment(monkeypatch, dbt_fails) -> None:
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "ambient_database")
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "ambient_token")
    connections = []
    dbt_calls = []

    def connect_scratch(config):
        connections.append(config)
        return Warehouse(connect(WarehouseConfig(":memory:")))

    def transform(*, target, selector, config):
        dbt_calls.append((target, selector))
        assert config is connections[0]
        assert config == WarehouseConfig("scratch_database", "scratch_token")
        assert os.environ["MOTHERDUCK_DATABASE"] == "ambient_database"
        assert os.environ["MOTHERDUCK_TOKEN"] == "ambient_token"
        if dbt_fails:
            raise RuntimeError("dbt failed")

    monkeypatch.setattr("personal_data_platform.recovery.rebuild.open_warehouse", connect_scratch)
    monkeypatch.setattr("personal_data_platform.recovery.rebuild.run_dbt", transform)

    def rebuild():
        return run_rebuild(
            SimpleNamespace(list_raw=lambda prefix: []),
            target_db="scratch_database",
            token="scratch_token",
            production_db="production_database",
            allow_partial_history=True,
            source=get_source("owntracks"),
        )

    if dbt_fails:
        with pytest.raises(RuntimeError, match="dbt failed"):
            rebuild()
    else:
        assert rebuild() == 0

    assert connections == [WarehouseConfig(database="scratch_database", token="scratch_token")]
    assert dbt_calls == [("prod", get_source("owntracks").dbt_selector)]
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


@pytest.mark.parametrize("source_id", ["iphone_screen_time", "mac_screen_time"])
def test_screen_time_rebuild_refuses_pending_only_history_before_connect(monkeypatch, source_id):
    def unexpected(*a, **k):
        raise AssertionError("must reject before connecting")

    monkeypatch.setattr("personal_data_platform.recovery.rebuild.open_warehouse", unexpected)
    with pytest.raises(ValueError, match="canonical base"):
        run_rebuild(
            SimpleNamespace(list_raw=unexpected),
            target_db="scratch",
            token="local",
            production_db="production",
            allow_partial_history=True,
            source=get_source(source_id),
        )


@pytest.mark.parametrize("missing_generation", [False, True])
def test_owntracks_rebuild_replays_one_pinned_inventory_into_empty_scratch(
    monkeypatch, tmp_path, missing_generation
):
    from personal_data_platform.recovery import rebuild
    from tests.integration.owntracks.test_location_point import Store

    source = get_source("owntracks")
    store = Store()
    raw = store.add()
    database = str(tmp_path / "scratch.duckdb")
    lists, dbt_calls = [], []
    original_list = store.list_raw

    def list_once(prefix):
        lists.append(prefix)
        return original_list(prefix)

    store.list_raw = list_once
    if missing_generation:

        def missing(key, *, generation):
            assert (key, generation) == (raw.key, 12)
            raise FileNotFoundError("generation removed")

        store.get_raw = missing
    monkeypatch.setattr(
        rebuild, "open_warehouse", lambda config: Warehouse(connect(WarehouseConfig(database)))
    )
    monkeypatch.setattr(rebuild, "run_dbt", lambda **kw: dbt_calls.append(kw))
    result = run_rebuild(
        store,
        target_db="scratch",
        token="local",
        production_db="production",
        allow_partial_history=True,
        source=source,
    )
    assert result == int(missing_generation)
    assert lists == list(source.raw_prefixes)
    assert len(dbt_calls) == int(not missing_generation)
    warehouse = Warehouse(connect(WarehouseConfig(database)))
    try:
        assert warehouse.location_point_ids() == (
            set() if missing_generation else {raw.logical_key}
        )
        with pytest.raises(ValueError, match="existing"):
            run_rebuild(
                store,
                target_db="scratch",
                token="local",
                production_db="production",
                allow_partial_history=True,
                source=source,
            )
    finally:
        warehouse.close()
