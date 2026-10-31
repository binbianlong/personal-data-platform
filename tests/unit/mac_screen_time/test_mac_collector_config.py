import pytest

from personal_data_platform.config import ConfigurationError


def test_mac_key_alone_enables_collection_and_uses_local_app_usage_directory(tmp_path) -> None:
    from personal_data_platform.sources.mac_screen_time.config import CollectorConfig

    environment = {"PDP_MAC_SCREEN_TIME_PSEUDONYM_KEY_HEX": "11" * 32}
    environment["PDP_MAC_SCREEN_TIME_DEVICE_KEY"] = "c" * 64
    environment["PDP_MAC_SCREEN_TIME_APP_USAGE_LOCAL_DIR"] = str(tmp_path / "AppUsage/local")

    config = CollectorConfig.from_env(environment)
    assert config.device_key == "c" * 64
    assert config.app_usage_local_dir == tmp_path / "AppUsage/local"

    environment["PDP_MAC_SCREEN_TIME_DEVICE_KEY"] = "raw-mac-identifier"
    with pytest.raises(ConfigurationError, match="DEVICE_KEY"):
        CollectorConfig.from_env(environment)
