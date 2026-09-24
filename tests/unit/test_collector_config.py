import ctypes
from pathlib import Path
from unittest.mock import Mock

import pytest

from personal_data_platform.config import ConfigurationError
from personal_data_platform.sources.screen_time.config import CollectorConfig


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PDP_PSEUDONYM_KEY_HEX": "11" * 32,
        "PDP_SYNC_DB_PATH": str(tmp_path / "sync.db"),
        "PDP_APP_IN_FOCUS_REMOTE_DIR": str(tmp_path / "remote"),
        "PDP_COLLECTOR_STATE_DB_PATH": str(tmp_path / "collector.db"),
        "PDP_SCREEN_TIME_DEVICE_ALLOWLIST": "a" * 64 + "," + "b" * 64,
        "GOOGLE_CLOUD_PROJECT": "synthetic-project",
        "GCS_BUCKET": "synthetic-bucket",
    }


def test_collector_configuration_loads_allowlist_without_raw_device_ids(tmp_path) -> None:
    config = CollectorConfig.from_env(_environment(tmp_path))

    assert config.device_allowlist == frozenset({"a" * 64, "b" * 64})
    assert config.pseudonym_key == bytes.fromhex("11" * 32)
    assert config.gcs is not None
    assert config.gcs.project_id == "synthetic-project"
    assert config.gcs.bucket == "synthetic-bucket"


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
        require_gcs=False,
        require_allowlist=False,
    )

    assert config.device_allowlist == frozenset()
    assert config.gcs is None


def test_collector_reads_existing_keychain_secret_in_python_process(tmp_path, monkeypatch) -> None:
    key_hex = b"42" * 32
    native_password = ctypes.create_string_buffer(key_hex)

    def find_password(
        _keychain,
        _service_length,
        _service,
        _account_length,
        _account,
        password_length,
        password_data,
        _item,
    ) -> int:
        ctypes.cast(password_length, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(key_hex)
        ctypes.cast(password_data, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.cast(
            native_password, ctypes.c_void_p
        ).value
        return 0

    native_library = Mock()
    native_library.SecKeychainFindGenericPassword.side_effect = find_password
    native_library.SecKeychainItemFreeContent.return_value = 0
    monkeypatch.setattr(ctypes, "CDLL", lambda _: native_library)
    environment = _environment(tmp_path)
    environment.pop("PDP_PSEUDONYM_KEY_HEX")

    config = CollectorConfig.from_env(environment)

    assert config.pseudonym_key == bytes.fromhex("42" * 32)
    native_library.SecKeychainItemFreeContent.assert_called_once()
