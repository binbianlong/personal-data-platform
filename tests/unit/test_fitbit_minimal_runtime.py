import pytest


def test_raw_adapter_rejects_legacy_decoding(monkeypatch):
    from personal_data_platform.sources.fitbit.adapter import FitbitSource

    assert FitbitSource().schema_versions == (3,)
    with pytest.raises(ValueError):
        FitbitSource(version=1)


def test_old_receipt_repair_command_is_removed():
    from personal_data_platform.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["fitbit", "repair"])
