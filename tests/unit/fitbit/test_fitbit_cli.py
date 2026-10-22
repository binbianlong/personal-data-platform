import pytest

from personal_data_platform.cli import main


def test_sync_requires_explicit_range():
    with pytest.raises(SystemExit):
        main(["fitbit", "sync"])


@pytest.mark.parametrize(
    "command",
    [
        ["loader", "--source", "fitbit"],
        ["rebuild", "--source", "fitbit", "--dry-run"],
        ["rebuild", "--source", "fitbit", "--all-streams", "--dry-run"],
    ],
)
def test_fitbit_raw_commands_redirect_to_api_sync(command, capsys, monkeypatch):
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    assert main(command) == 1
    assert "fitbit sync" in capsys.readouterr().err


def test_sync_preserves_narrow_physical_bounds(monkeypatch):
    from datetime import datetime

    from personal_data_platform.sources.fitbit import runtime

    calls = []
    monkeypatch.setattr(runtime, "run_sync_from_env", lambda **kwargs: calls.append(kwargs) or 0)
    assert (
        main(
            [
                "fitbit",
                "sync",
                "--from",
                "2020-01-01T00:01:00Z",
                "--to",
                "2020-01-01T00:03:00Z",
                "--data-type",
                "steps",
            ]
        )
        == 0
    )
    assert calls == [
        {
            "start": datetime.fromisoformat("2020-01-01T00:01:00+00:00"),
            "end": datetime.fromisoformat("2020-01-01T00:03:00+00:00"),
            "data_types": ("steps",),
        }
    ]


def test_worker_command_starts_push_service(monkeypatch):
    from personal_data_platform.sources.fitbit import runtime

    monkeypatch.setattr(runtime, "run_worker_from_env", lambda: 0)
    assert main(["fitbit", "serve-worker"]) == 0
