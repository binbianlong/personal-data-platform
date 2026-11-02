"""Both local collectors drain through the production Warehouse transaction boundary."""

from contextlib import closing

import pytest

from personal_data_platform.biome import runtime
from personal_data_platform.sources.registry import get_source
from personal_data_platform.storage.motherduck import Warehouse, WarehouseConfig, connect
from personal_data_platform.storage.warehouse import WarehouseConnectionError, WarehouseLeaseError
from tests.contract.test_mac_app_usage_collector import _collectors


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("failure", [None, "expiry", "commit-after", "ack"])
def test_both_collectors_preserve_pending_until_real_warehouse_commit(
    tmp_path, monkeypatch, index, failure
):
    collectors, states = _collectors(tmp_path)
    collector, state = collectors[index], states[index]
    source = get_source(state.source_id)
    collector.collect_once(parser_version=source.parser_version)
    pending = state.pending()[0]
    path = str(tmp_path / "warehouse.duckdb")
    warehouse = Warehouse(connect(WarehouseConfig(path)))
    warehouse.migrate()
    ack = state.mark_loaded
    if failure == "ack":
        monkeypatch.setattr(
            state, "mark_loaded", lambda *a, **k: (_ for _ in ()).throw(OSError("ACK failed"))
        )
    if failure == "expiry":
        from personal_data_platform.sources.iphone_screen_time import motherduck as phone
        from personal_data_platform.sources.mac_screen_time import motherduck as mac

        module = phone if index == 0 else mac
        write = module.write_state

        def expire(connection, *args, **kwargs):
            count = write(connection, *args, **kwargs)
            connection.execute("UPDATE ops.job_lock SET expires_at=TIMESTAMPTZ '2000-01-01'")
            return count

        monkeypatch.setattr(module, "write_state", expire)
    if failure == "commit-after":
        connection = warehouse.connection

        class LostCommitResponse:
            def execute(self, sql, *args):
                result = connection.execute(sql, *args)
                if (
                    sql == "COMMIT"
                    and connection.execute(
                        f"SELECT count(*) FROM base.{source.source_id}_event"
                    ).fetchone()[0]
                ):
                    raise OSError("commit response lost")
                return result

            def __getattr__(self, name):
                return getattr(connection, name)

        warehouse.connection = LostCommitResponse()
    try:
        if failure:
            with pytest.raises((OSError, WarehouseLeaseError, WarehouseConnectionError)):
                runtime.load_pending(state, warehouse, source=source)
            assert [p.identity for p in state.pending()] == [pending.identity]
            assert state.pending()[0].compressed_payload == pending.compressed_payload
            assert warehouse.query_value(
                f"SELECT count(*) FROM base.{source.source_id}_event"
            ) == int(failure in {"commit-after", "ack"})
        else:
            runtime.load_pending(state, warehouse, source=source)
            assert not state.pending()
    finally:
        warehouse.close()
    if failure == "expiry":
        monkeypatch.setattr(module, "write_state", write)
    if failure == "ack":
        monkeypatch.setattr(state, "mark_loaded", ack)
    # Reopening clears unknown connection state; an outstanding lease is not bypassed.
    with closing(Warehouse(connect(WarehouseConfig(path)))) as fresh:
        fresh.connection.execute("UPDATE ops.job_lock SET expires_at=TIMESTAMPTZ '2000-01-01'")
        runtime.load_pending(state, fresh, source=source)
        runtime.load_pending(state, fresh, source=source)
        assert not state.pending()
        assert fresh.query_value(f"SELECT count(*) FROM base.{source.source_id}_event") == 1
        other = states[1 - index].source_id
        assert fresh.query_value(f"SELECT count(*) FROM base.{other}_event") == 0
        with state._connect() as local:
            assert local.execute("SELECT count(*) FROM event_reference").fetchone()[0] == 1
            assert local.execute("SELECT status,payload_path FROM file_state").fetchall() == [
                ("sent", None)
            ]
