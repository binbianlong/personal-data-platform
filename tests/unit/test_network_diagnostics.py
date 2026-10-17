import json

import duckdb
import pytest

from personal_data_platform import network_diagnostics as net


@pytest.fixture(autouse=True)
def isolated_diagnostic_logger(monkeypatch):
    monkeypatch.setattr(net.LOGGER, "handlers", [])
    monkeypatch.setattr(net.LOGGER, "level", net.LOGGER.level)
    monkeypatch.setattr(net.LOGGER, "propagate", net.LOGGER.propagate)


class Counter:
    def __init__(self):
        self.sent = 100
        self.received = 200
        self.broken = False

    def read(self):
        if self.broken:
            raise OSError("unavailable")
        return net.Counters(self.sent, self.received, ("eth0",))


@pytest.fixture
def counter(monkeypatch):
    value = Counter()
    monkeypatch.setenv("PDP_NETWORK_DIAGNOSTICS", "true")
    monkeypatch.setattr(net, "read_counters", value.read)
    return value


def test_network_counter_excludes_loopback_and_uses_transmit_column(tmp_path):
    path = tmp_path / "dev"
    path.write_text(
        "Inter-| Receive | Transmit\n face |bytes packets\n"
        "lo: 900 0 0 0 0 0 0 0 800 0 0 0 0 0 0 0\n"
        "eth0: 120 0 0 0 0 0 0 0 45 0 0 0 0 0 0 0\n"
        "eth1: 30 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\n"
    )
    assert net.read_counters(path) == net.Counters(
        50, 150, ("eth0", "eth1"), (("eth0", 45, 120), ("eth1", 5, 30))
    )


def test_nested_phases_account_for_each_byte_once(counter):
    with net.network_run("test") as profile:
        with net.network_phase("outer"):
            counter.sent += 10
            with net.network_phase("inner"):
                counter.sent += 20
                counter.received += 7
            counter.sent += 5
        counter.sent += 3
    summary = profile.summary()
    assert summary["sent_bytes"] == 38
    assert summary["received_bytes"] == 7
    phases = {row["phase"]: row for row in summary["phases"]}
    assert phases["outer"]["sent_bytes"] == 15
    assert phases["outer/inner"]["sent_bytes"] == 20
    assert phases["unattributed"]["sent_bytes"] == 3
    assert sum(row["sent_bytes"] for row in phases.values()) == 38


def test_reset_on_one_interface_is_not_hidden_by_growth_on_another(tmp_path, monkeypatch):
    path = tmp_path / "dev"
    header = "Inter-| Receive | Transmit\n face |bytes packets\n"
    path.write_text(
        header
        + "eth0: 100 0 0 0 0 0 0 0 100 0 0 0 0 0 0 0\n"
        + "eth1: 100 0 0 0 0 0 0 0 100 0 0 0 0 0 0 0\n"
    )
    reader = net.read_counters
    monkeypatch.setenv("PDP_NETWORK_DIAGNOSTICS", "true")
    monkeypatch.setattr(net, "read_counters", lambda: reader(path))
    with net.network_run("test") as profile:
        path.write_text(
            header
            + "eth0: 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
            + "eth1: 100 0 0 0 0 0 0 0 250 0 0 0 0 0 0 0\n"
        )
    assert profile.summary()["valid"] is False
    assert profile.summary()["sent_bytes"] is None


@pytest.mark.parametrize("failure", ["unavailable", "reset", "interfaces_changed"])
def test_bad_counter_does_not_report_partial_or_zero_success(counter, monkeypatch, failure):
    with net.network_run("test") as profile:
        with net.network_phase("coverage_write"):
            counter.sent += 50
        if failure == "unavailable":
            counter.broken = True
        elif failure == "reset":
            counter.sent = 0
        else:
            monkeypatch.setattr(net, "read_counters", lambda: net.Counters(200, 300, ("eth1",)))
            profile.reader = net.read_counters
    summary = profile.summary()
    assert summary["valid"] is False
    assert summary["sent_bytes"] is None
    assert all(row["sent_bytes"] is None for row in summary["phases"])


def test_probe_failure_never_masks_ingestion_failure(counter):
    with pytest.raises(ValueError, match="original failure"):
        with net.network_run("test") as profile:
            with net.network_phase("data_write"):
                counter.broken = True
                raise ValueError("original failure")
    assert profile.summary()["valid"] is False


def test_disabled_measurement_does_not_read_counters_or_wrap_connection(monkeypatch):
    monkeypatch.delenv("PDP_NETWORK_DIAGNOSTICS", raising=False)
    monkeypatch.setattr(net, "read_counters", lambda: pytest.fail("counter was read"))
    connection = duckdb.connect()
    try:
        with net.network_run("test") as profile:
            assert profile is None
            assert net.instrument_connection(connection) is connection
            with net.network_phase("data_write"):
                assert connection.execute("SELECT 7").fetchone() == (7,)
    finally:
        connection.close()


def test_sql_probe_preserves_transactions_results_and_hides_payloads(counter):
    with net.network_run("test") as profile:
        connection = net.instrument_connection(duckdb.connect())
        connection.execute("CREATE SCHEMA ops")
        connection.execute("CREATE TABLE ops.fitbit_coverage (value VARCHAR)")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO ops.fitbit_coverage VALUES (?)", ["private-health-value"])
        connection.execute("ROLLBACK")
        assert connection.execute("SELECT * FROM ops.fitbit_coverage").fetchall() == []
        with pytest.raises(duckdb.Error):
            connection.execute("SELECT * FROM missing_table")
        connection.execute("INSERT INTO ops.fitbit_coverage VALUES (?)", ["synthetic-secret"])
        assert connection.execute("SELECT * FROM ops.fitbit_coverage").fetchone() == (
            "synthetic-secret",
        )
        connection.close()
    encoded = json.dumps(profile.summary())
    assert "private-health-value" not in encoded
    assert "synthetic-secret" not in encoded
    assert "missing_table" not in encoded
    phases = {row["phase"] for row in profile.summary()["phases"]}
    assert {
        "coverage_read",
        "coverage_write",
        "transaction_begin",
        "transaction_rollback",
    } <= phases


def test_connection_execute_and_fetch_are_attributed_to_same_category(counter):
    class RemoteConnection:
        def execute(self, query, parameters=None):
            assert parameters == ["private"]
            counter.sent += 100
            return self

        def fetchall(self):
            counter.sent += 9
            counter.received += 200
            return [(1,)]

    with net.network_run("test") as profile:
        connection = net.instrument_connection(RemoteConnection())
        assert connection.execute(
            "SELECT * FROM ops.job_lock WHERE owner_id=?", ["private"]
        ).fetchall() == [(1,)]
    row = next(row for row in profile.summary()["phases"] if row["phase"] == "lock_check")
    assert row["sent_bytes"] == 109
    assert row["received_bytes"] == 200


def test_nested_runs_share_outer_profile(counter):
    with net.network_run("daily") as outer:
        with net.network_run("nested") as inner:
            assert outer is inner
            counter.sent += 8
    assert outer.summary()["sent_bytes"] == 8


def test_warehouse_profiles_connect_schema_and_lock_without_changing_results(counter):
    from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect

    with net.network_run("test") as profile:
        warehouse = Warehouse(connect(WarehouseConfig(":memory:")))
        warehouse.migrate()
        assert warehouse.acquire_job_lock("loader", "owner", lease_seconds=600)
        warehouse.require_job_lock("owner")
        warehouse.release_job_lock("loader", "owner")
        assert warehouse.query_value("SELECT count(*) FROM ops.job_lock") == 0
        warehouse.close()
    phases = {row["phase"] for row in profile.summary()["phases"]}
    assert {"db_connect", "schema_check", "lock_check", "lock_write", "db_close"} <= phases


def test_dbt_run_and_test_have_separate_network_phases(counter, monkeypatch):
    from personal_data_platform import dbt_runner

    def invoke(arguments):
        counter.sent += 110 if arguments[0] == "run" else 70

    monkeypatch.setattr(dbt_runner, "_invoke", invoke)
    with net.network_run("daily") as profile:
        dbt_runner.run_dbt(target="local")
    phases = {row["phase"]: row for row in profile.summary()["phases"]}
    assert phases["dbt_run"]["sent_bytes"] == 110
    assert phases["dbt_test"]["sent_bytes"] == 70
