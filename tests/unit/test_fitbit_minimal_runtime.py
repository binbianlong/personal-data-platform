import pytest


@pytest.mark.parametrize(
    "command",
    [
        ["loader", "--source", "fitbit"],
        ["rebuild", "--source", "fitbit", "--dry-run"],
        ["rebuild", "--source", "fitbit", "--all-streams", "--dry-run"],
    ],
)
def test_fitbit_raw_commands_redirect_to_api_sync(command, capsys, monkeypatch):
    from personal_data_platform.cli import main

    monkeypatch.delenv("GCS_BUCKET", raising=False)
    assert main(command) == 1
    assert "fitbit sync" in capsys.readouterr().err


def test_old_receipt_repair_command_is_removed():
    from personal_data_platform.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["fitbit", "repair"])
