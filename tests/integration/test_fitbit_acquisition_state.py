from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from personal_data_platform.storage.motherduck import Warehouse


@pytest.fixture
def warehouse():
    value = Warehouse(duckdb.connect())
    value.migrate()
    yield value
    value.close()


def setup_state(warehouse):
    from personal_data_platform.sources.fitbit.acquisition_state import AcquisitionState
    from personal_data_platform.sources.fitbit.models import Notification, Window

    when = datetime(2026, 10, 1, tzinfo=UTC)
    window = Window("steps", when, when + timedelta(days=1))
    return AcquisitionState(warehouse), Notification, window, when


def finish(warehouse, state, attempt, digest="a" * 64, keys=("raw/fitbit/v2/a",)):
    warehouse.connection.execute("BEGIN")
    state.finish_attempt(attempt, source_sha256=digest, raw_keys=keys)
    warehouse.connection.execute("COMMIT")


def test_old_success_does_not_complete_new_notification(warehouse):
    state, Notification, window, when = setup_state(warehouse)
    old = Notification("old", "self", (window,), when)
    scope = state.register_notifications((old,))["old"][0]
    attempt = state.start_attempt(scope, started_at=when + timedelta(seconds=1))
    state.bind_attempt(("old",), scope, attempt)
    finish(warehouse, state, attempt)
    assert state.ackable_ids(("old",)) == frozenset({"old"})
    new = Notification("new", "self", (window,), when + timedelta(minutes=1))
    state.register_notifications((new,))
    assert state.ackable_ids(("new",)) == frozenset()
    with pytest.raises(ValueError, match="received"):
        state.bind_attempt(("new",), scope, attempt)


def test_interrupted_notification_registration_rolls_back_all_scopes(warehouse, monkeypatch):
    state, Notification, window, when = setup_state(warehouse)
    other = type(window)("sleep", window.start, window.end)
    notification = Notification("retry", "self", (window, other), when)
    original = state._ensure_scope
    calls = []

    def interrupted(scope):
        calls.append(scope)
        if len(calls) == 2:
            raise RuntimeError("registration interrupted")
        original(scope)

    with monkeypatch.context() as patch:
        patch.setattr(state, "_ensure_scope", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            state.register_notifications((notification,))
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_notification") == 0
    assert warehouse.query_value("SELECT count(*) FROM ops.fitbit_notification_scope") == 0
    assert len(state.register_notifications((notification,))["retry"]) == 2
    assert len(state.register_notifications((notification,))["retry"]) == 2


def test_ack_waits_for_all_scopes_and_commit(warehouse):
    from personal_data_platform.sources.fitbit.models import Window

    state, Notification, window, when = setup_state(warehouse)
    second = Window("sleep", window.start, window.end)
    scopes = state.register_notifications((Notification("n", "self", (window, second), when),))["n"]
    attempts = []
    for scope in scopes:
        attempt = state.start_attempt(scope, started_at=when + timedelta(seconds=1))
        state.bind_attempt(("n",), scope, attempt)
        attempts.append(attempt)
    finish(warehouse, state, attempts[0])
    assert state.ackable_ids(("n",)) == frozenset()
    warehouse.connection.execute("BEGIN")
    state.finish_attempt(attempts[1], source_sha256="b" * 64, raw_keys=("raw/second",))
    warehouse.connection.execute("ROLLBACK")
    assert state.ackable_ids(("n",)) == frozenset()
    finish(warehouse, state, attempts[1], "b" * 64, ("raw/second",))
    assert state.ackable_ids(("n", "unknown")) == frozenset({"n"})


def test_redelivery_preserves_committed_attempt_after_reconnect(tmp_path):
    from personal_data_platform.sources.fitbit.acquisition_state import AcquisitionState

    path = str(tmp_path / "state.duckdb")
    warehouse = Warehouse(duckdb.connect(path))
    warehouse.migrate()
    state, Notification, window, when = setup_state(warehouse)
    notification = Notification("n", "self", (window,), when)
    scope = state.register_notifications((notification,))["n"][0]
    attempt = state.start_attempt(scope, started_at=when + timedelta(seconds=1))
    state.bind_attempt(("n",), scope, attempt)
    finish(warehouse, state, attempt)
    warehouse.close()
    reopened = Warehouse(duckdb.connect(path))
    try:
        state = AcquisitionState(reopened)
        state.register_notifications((notification,))
        assert state.ackable_ids(("n",)) == frozenset({"n"})
        assert state.latest_success(scope).source_sha256 == "a" * 64
    finally:
        reopened.close()


def test_bundle_intent_is_idempotent_and_rejects_changed_bytes(warehouse):
    state, Notification, window, when = setup_state(warehouse)
    scope = state.register_notifications((Notification("n", "self", (window,), when),))["n"][0]
    attempt = state.start_attempt(scope, started_at=when)
    chunks = (("raw/fitbit/v2/one", "a" * 64, 100), ("raw/fitbit/v2/two", "b" * 64, 200))
    state.prepare_bundle("bundle", (attempt,), chunks)
    state.prepare_bundle("bundle", (attempt,), chunks)
    assert state.pending_bundles()[0].chunks == chunks
    with pytest.raises(ValueError, match="intent"):
        state.prepare_bundle("bundle", (attempt,), (("raw/fitbit/v2/one", "c" * 64, 100),))
    assert len(state.pending_bundles()[0].chunks) == 2


def test_notification_identity_cannot_change_its_required_scope(warehouse):
    from personal_data_platform.sources.fitbit.models import Window

    state, Notification, window, when = setup_state(warehouse)
    state.register_notifications((Notification("n", "self", (window,), when),))
    with pytest.raises(ValueError, match="identity"):
        state.register_notifications(
            (Notification("n", "self", (Window("sleep", window.start, window.end),), when),)
        )


def test_incomplete_intent_retires_only_after_all_scopes_are_refetched(warehouse):
    from personal_data_platform.sources.fitbit.models import Window

    state, Notification, window, when = setup_state(warehouse)
    scopes = state.register_notifications(
        (Notification("n", "self", (window, Window("sleep", window.start, window.end)), when),)
    )["n"]
    old_attempts = tuple(state.start_attempt(scope, started_at=when) for scope in scopes)
    state.prepare_bundle("incomplete", old_attempts, (("raw/fitbit/v2/missing", "a" * 64, 100),))
    state.retire_superseded_bundles()
    assert len(state.pending_bundles()) == 1
    for index, scope in enumerate(scopes):
        newer = state.start_attempt(scope, started_at=when + timedelta(hours=1))
        finish(warehouse, state, newer, keys=("raw/new",))
        state.retire_superseded_bundles()
        assert len(state.pending_bundles()) == (1 if index == 0 else 0)
    assert (
        warehouse.query_value("SELECT status FROM ops.fitbit_bundle WHERE bundle_id='incomplete'")
        == "superseded"
    )
    assert (
        warehouse.query_value("SELECT count(*) FROM ops.fitbit_attempt WHERE status='succeeded'")
        == 2
    )
