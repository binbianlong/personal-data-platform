import pytest


def test_normal_runtime_uses_only_west_profile(monkeypatch):
    from personal_data_platform.config import ConfigurationError, schema_profile

    monkeypatch.delenv("PDP_SCHEMA_PROFILE", raising=False)
    assert schema_profile() == "west"
    monkeypatch.setenv("PDP_SCHEMA_PROFILE", "legacy")
    with pytest.raises(ConfigurationError):
        schema_profile()


def test_legacy_delivery_mode_is_rejected(monkeypatch):
    from personal_data_platform.sources.fitbit.runtime import delivery_mode

    monkeypatch.setenv("PDP_FITBIT_DELIVERY_MODE", "legacy")
    with pytest.raises(ValueError):
        delivery_mode()


def test_raw_adapter_rejects_legacy_decoding(monkeypatch):
    from personal_data_platform.sources.fitbit.adapter import FitbitSource

    monkeypatch.delenv("PDP_FITBIT_DELIVERY_MODE", raising=False)
    monkeypatch.delenv("PDP_SCHEMA_PROFILE", raising=False)
    assert FitbitSource().schema_versions == (3,)
    with pytest.raises(ValueError):
        FitbitSource(version=1)


def test_old_receipt_repair_command_is_removed():
    from personal_data_platform.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["fitbit", "repair"])
