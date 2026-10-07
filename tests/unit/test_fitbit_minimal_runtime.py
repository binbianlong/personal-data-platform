import pytest


def test_normal_runtime_uses_only_west_profile(monkeypatch):
    from personal_data_platform.config import ConfigurationError, schema_profile

    monkeypatch.delenv("PDP_SCHEMA_PROFILE", raising=False)
    assert schema_profile() == "west"
    monkeypatch.setenv("PDP_SCHEMA_PROFILE", "legacy")
    with pytest.raises(ConfigurationError):
        schema_profile()


def test_raw_adapter_rejects_legacy_decoding(monkeypatch):
    from personal_data_platform.sources.fitbit.adapter import FitbitSource

    monkeypatch.delenv("PDP_SCHEMA_PROFILE", raising=False)
    assert FitbitSource().schema_versions == (3,)
    with pytest.raises(ValueError):
        FitbitSource(version=1)


def test_old_receipt_repair_command_is_removed():
    from personal_data_platform.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["fitbit", "repair"])
