from personal_data_platform.cli import main


def test_migrate_can_initialize_an_explicit_local_database(tmp_path):
    import duckdb

    database = tmp_path / "local.duckdb"
    assert main(["migrate", "--database", str(database)]) == 0
    with duckdb.connect(str(database)) as connection:
        assert connection.execute("select count(*) from base.fitbit_steps").fetchone() == (0,)


def test_sync_requires_explicit_range_and_rejects_resume_id():
    import pytest

    for args in (["fitbit", "sync"], ["fitbit", "sync", "--resume-id", "saved-range"]):
        with pytest.raises(SystemExit):
            main(args)


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


def test_deferred_hourly_job_exits_zero_and_reports_unfinished_scope(monkeypatch, capsys):
    import json
    from datetime import UTC, datetime, timedelta

    from personal_data_platform.sources.fitbit import runtime
    from personal_data_platform.sources.fitbit.acquisition import AcquisitionSummary
    from personal_data_platform.sources.fitbit.models import Window

    at = datetime(2026, 10, 1, tzinfo=UTC)
    result = AcquisitionSummary(
        deferred_scopes=1, first_incomplete=Window("steps", at, at + timedelta(days=1))
    )
    monkeypatch.setattr(runtime, "run_notification_job", lambda **kwargs: result)
    assert main(["fitbit", "ingest-notifications"]) == 0
    assert json.loads(capsys.readouterr().out)["first_incomplete"]["data_type"] == "steps"
