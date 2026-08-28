from pathlib import Path

import pytest

from personal_data_platform.config import CollectorConfig, ConfigurationError


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PDP_PSEUDONYM_KEY_HEX": "11" * 32,
        "PDP_SYNC_DB_PATH": str(tmp_path / "sync.db"),
        "PDP_APP_IN_FOCUS_REMOTE_DIR": str(tmp_path / "remote"),
        "PDP_COLLECTOR_STATE_DB_PATH": str(tmp_path / "collector.db"),
        "PDP_SCREEN_TIME_DEVICE_ALLOWLIST": "a" * 64 + "," + "b" * 64,
        "B2_ENDPOINT": "https://s3.example.invalid",
        "B2_KEY_ID": "synthetic-key-id",
        "B2_APPLICATION_KEY": "synthetic-application-key",
        "B2_BUCKET": "synthetic-bucket",
    }


def test_collector_configuration_loads_allowlist_without_raw_device_ids(tmp_path) -> None:
    config = CollectorConfig.from_env(_environment(tmp_path))

    assert config.device_allowlist == frozenset({"a" * 64, "b" * 64})
    assert config.pseudonym_key == bytes.fromhex("11" * 32)
    assert config.b2 is not None
    assert config.b2.bucket == "synthetic-bucket"


def test_collection_requires_nonempty_device_allowlist(tmp_path) -> None:
    environment = _environment(tmp_path)
    environment["PDP_SCREEN_TIME_DEVICE_ALLOWLIST"] = ""

    with pytest.raises(ConfigurationError, match="DEVICE_ALLOWLIST is required"):
        CollectorConfig.from_env(environment)


def test_devices_command_configuration_can_load_before_allowlist_is_chosen(tmp_path) -> None:
    environment = _environment(tmp_path)
    environment.pop("PDP_SCREEN_TIME_DEVICE_ALLOWLIST")

    config = CollectorConfig.from_env(
        environment,
        require_b2=False,
        require_allowlist=False,
    )

    assert config.device_allowlist == frozenset()
    assert config.b2 is None
