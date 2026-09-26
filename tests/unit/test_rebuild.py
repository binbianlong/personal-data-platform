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
    run_rebuild_all,
    run_rebuild_from_env,
    validate_rebuild_target,
)
from personal_data_platform.sources.registry import get_source
from personal_data_platform.sources.screen_time.raw import ScreenTimeRawIdentity
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from tests.screen_time_helpers import Repository, event, mac_usage_event, segb


def test_all_streams_rebuilds_one_scratch_database_and_shared_daily_views(
    tmp_path, monkeypatch
) -> None:
    from personal_data_platform.dbt_runner import run_dbt as real_run_dbt

    database = tmp_path / "combined.duckdb"
    monkeypatch.setenv("DBT_DUCKDB_PATH", str(database))
    monkeypatch.setattr(
        "personal_data_platform.recovery.rebuild.connect",
        lambda _config: connect(WarehouseConfig(str(database))),
    )
    calls = []

    def build_views(*, target, selector):
        calls.append((target, selector))
        real_run_dbt(target="local", selector=selector)

    monkeypatch.setattr("personal_data_platform.recovery.rebuild.run_dbt", build_views)
    when = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)
    phone_repository = Repository()
    mac_repository = Repository()
    for name, payload in (
        ("100", event("phone.app", 100.0)),
        ("200", event("phone.app", 160.0, foreground=False)),
    ):
        phone_repository.add(name, segb(payload)[0])
    for name, payload in (
        ("100", mac_usage_event("mac.app", when.timestamp(), start=True)),
        ("200", mac_usage_event("mac.app", when.timestamp() + 60, start=False)),
    ):
        mac_repository.add(name, segb(payload)[0], device="b" * 64, stream="app-usage")
    phone = get_source("screen_time", "app-in-focus")
    mac = get_source("screen_time", "app-usage")
    inventories = (
        (phone, phone_repository, tuple(value[0] for value in phone_repository.objects.values())),
        (mac, mac_repository, tuple(value[0] for value in mac_repository.objects.values())),
    )

    assert (
        run_rebuild_all(
            inventories,
            target_db="scratch",
            token="local",
            production_db="production",
            allow_partial_history=True,
        )
        == 0
    )
    assert calls == [("prod", "tag:screen_time")]
    warehouse = Warehouse(connect(WarehouseConfig(str(database))))
    try:
        assert warehouse.query_rows(
            "SELECT platform, count(*) FROM base.screen_time_event GROUP BY platform ORDER BY platform"
        ) == [("ios", 2), ("macos", 2)]
        assert warehouse.query_rows(
            "SELECT platform, total_seconds FROM marts.daily_screen_time_total ORDER BY platform"
        ) == [("ios", 60.0), ("macos", 60.0)]
    finally:
        warehouse.close()


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

    def repository_from_env(**_):
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(rebuild_adc.resolve())
        return Repository()

    monkeypatch.setattr(
        "personal_data_platform.sources.screen_time.storage.ScreenTimeGCSRepository.from_env",
        repository_from_env,
    )

    assert run_rebuild_from_env(dry_run=True, target_db=None) == 0
    assert prefixes == ["raw/screen_time/v1/", "raw/screen_time/v2/"]
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

    def transform(*, target, selector):
        dbt_calls.append((target, selector))
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
    assert dbt_calls == [("prod", get_source().dbt_selector)]
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
    raw = _raw("disappeared", "a", observed_at, observed_at, generation=9)

    class Repository:
        def __init__(self) -> None:
            self.list_calls = 0

        def list_raw(self, prefix):
            self.list_calls += 1
            return [raw] if raw.key.startswith(prefix) else []

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
    assert repository.list_calls == 2


def test_rebuild_keeps_the_selected_source_stream_and_all_schema_generations(monkeypatch):
    import gzip
    from dataclasses import replace

    from personal_data_platform.sources.contracts import SourceHealth

    observed_at = datetime(2026, 8, 27, tzinfo=UTC)
    payload = b"synthetic-payload"
    observations = [
        RawObject(
            key=f"raw/synthetic/v{version}/metrics/account/day.json.gz",
            source_id="synthetic",
            schema_version=version,
            subject_key="account",
            stream="metrics",
            logical_key="day",
            observed_at=observed_at,
            sha256=hashlib.sha256(payload).hexdigest(),
            storage_created_at=observed_at,
            storage_generation=version + 10,
        )
        for version in (1, 2)
    ]
    listed = []
    read = []
    written = []
    dbt_calls = []

    class Batch:
        parser_version = "synthetic-1"
        record_count = 1

        def write(self, connection, raw, *, byte_size, loaded_at):
            written.append(raw)

    class Source:
        source_id = "synthetic"
        stream = "metrics"
        schema_versions = (1, 2)
        raw_prefixes = ("raw/synthetic/v1/metrics/", "raw/synthetic/v2/metrics/")
        raw_suffixes = (".json.gz",)
        retention_days = 7
        lifecycle_grace_days = 2
        required_relations = ()
        dbt_selector = "tag:synthetic_metrics"
        monitor_name = "synthetic_metrics_reconciliation"

        def validate_raw_key(self, key):
            assert key.startswith(self.raw_prefixes)

        def parse_raw_key(self, key, *, storage_created_at, storage_generation):
            return replace(
                next(raw for raw in observations if raw.key == key),
                storage_created_at=storage_created_at,
                storage_generation=storage_generation,
            )

        def decode(self, raw, value):
            assert value == payload
            return Batch()

        def audit(self, repository, values, now):
            return SourceHealth(ok=True, details={})

        def inventory(self, values):
            return {"synthetic_count": len(values)}

    class Repository:
        def list_raw(self, prefix):
            listed.append(prefix)
            return [raw for raw in observations if raw.key.startswith(prefix)]

        def get_raw(self, key, *, generation):
            read.append((key, generation))
            return gzip.compress(payload)

    source = Source()
    monkeypatch.setattr(
        "personal_data_platform.recovery.rebuild.connect",
        lambda config: connect(WarehouseConfig(":memory:")),
    )
    monkeypatch.setattr(
        "personal_data_platform.recovery.rebuild.run_dbt",
        lambda **kwargs: dbt_calls.append(kwargs),
    )

    assert (
        run_rebuild(
            Repository(),
            target_db="scratch_database",
            token="scratch_token",
            production_db="production_database",
            allow_partial_history=True,
            source=source,
        )
        == 0
    )

    assert listed == list(source.raw_prefixes)
    assert read == [(raw.key, raw.storage_generation) for raw in observations]
    assert written == observations
    assert dbt_calls == [{"target": "prod", "selector": source.dbt_selector}]
    inventory = rebuild_inventory(observations, source=source)
    assert inventory["source_id"] == "synthetic"
    assert inventory["stream"] == "metrics"
    assert inventory["schema_versions"] == [1, 2]
    assert inventory["retention_days"] == 7
    assert inventory["subject_count"] == inventory["scope_count"] == 1
    assert inventory["synthetic_count"] == 2
    assert "device_count" not in inventory


@pytest.mark.parametrize(
    ("name", "value"), [("PDP_RAW_RETENTION_DAYS", "7"), ("PDP_LIFECYCLE_GRACE_DAYS", "0")]
)
def test_rebuild_rejects_deployment_retention_drift_before_cloud_access(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "personal_data_platform.sources.screen_time.adapter.ScreenTimeSource.repository_from_env",
        lambda _: pytest.fail("repository must not open with mismatched retention"),
    )

    with pytest.raises(ValueError, match=name):
        run_rebuild_from_env(dry_run=True, target_db=None)
