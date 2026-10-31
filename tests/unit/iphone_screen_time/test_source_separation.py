import importlib
import importlib.util

import pytest


@pytest.mark.parametrize(
    "source_id,device_setting",
    [("iphone_screen_time", "DEVICE_ALLOWLIST"), ("mac_screen_time", "DEVICE_KEY")],
)
def test_independent_configuration_does_not_read_counterpart_settings(
    source_id, device_setting, tmp_path
):
    assert importlib.util.find_spec(f"personal_data_platform.sources.{source_id}") is not None
    module = importlib.import_module(f"personal_data_platform.sources.{source_id}.config")
    prefix = f"PDP_{source_id.upper()}_"
    config = module.CollectorConfig.from_env(
        {prefix + "PSEUDONYM_KEY_HEX": "11" * 32, prefix + device_setting: "a" * 64}
    )
    assert config.state_db_path.name == "collector.db"
    assert config.state_db_path.parent.name == source_id
    assert config.poll_seconds == 1800


@pytest.mark.parametrize(
    "source_id,adapter_name,stream",
    [
        ("iphone_screen_time", "IPhoneScreenTimeSource", "app-in-focus"),
        ("mac_screen_time", "MacScreenTimeSource", "app-usage"),
    ],
)
def test_source_adapter_owns_only_its_raw_namespace(source_id, adapter_name, stream):
    assert importlib.util.find_spec(f"personal_data_platform.sources.{source_id}") is not None
    adapter = getattr(
        importlib.import_module(f"personal_data_platform.sources.{source_id}.adapter"), adapter_name
    )()
    assert adapter.source_id == source_id
    assert adapter.stream == stream
    assert adapter.raw_prefixes == (f"raw/{source_id}/v2/",)
    assert adapter.monitor_name == source_id + "_reconciliation"
