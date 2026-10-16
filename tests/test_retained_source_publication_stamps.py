"""Receipt-only retained source stamps, with synthetic clocks and private SQL."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import sqlite3
import threading

import pytest

from service.publication_authority import Authority, Clock, PublicationBusy, Witness, publication
from service.assistant import scheduler
from service.assistant import store as store_module


@pytest.fixture
def env(tmp_path, monkeypatch):
    now = [1800000000.0, 10000.0]
    clock = Clock(live=False)
    clock.observe(*now)
    monkeypatch.setattr(scheduler.time, "time", lambda: now[0])
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: now[1])
    monkeypatch.setattr(scheduler, "LIVE_CLOCK", clock)
    monkeypatch.setattr(store_module, "LIVE_CLOCK", clock)
    monkeypatch.setattr(scheduler, "_sync_status", {})
    monkeypatch.setattr(scheduler, "_behavior_calendar_authority", Authority("test-calendar"))
    monkeypatch.setattr(scheduler, "_behavior_calendar_metadata", None)
    stores = []
    def make(path=None):
        result = store_module.AssistantStore(path or tmp_path / "native.db")
        stores.append(result)
        return result
    def advance(seconds):
        now[0] += seconds
        now[1] += seconds
        clock.observe(*now)
    def diagnostics(**changes):
        return dict(authorized=True, snapshot_started_at=now[0]-1, complete=True, partial=False,
                    coverage_start=now[0]-86400, coverage_end=now[0]+86400) | changes
    def metadata(store):
        day = datetime.fromtimestamp(now[0], timezone.utc).date().isoformat()
        return store.today_snapshot(day, "UTC")["sources"].get("reminders", {})
    yield now, clock, make, advance, diagnostics, metadata
    for store in stores:
        store._db.close()


def calendar_publish(env, **changes):
    scheduler.record_sync("calendar", 0, env[4](**changes))
    return scheduler.connectors_status()["calendar"]


def reminders_publish(env, store, **changes):
    store.sync_source("reminders", [], diagnostics=env[4](**changes))
    return env[5](store)


def test_calendar_requires_actual_process_issued_current_permission(env):
    now = env[0][0]
    forged = dict(available=True, syncing=False, last_sync=now,
                  diagnostics=env[4]())
    assert scheduler.prepare_behavior_source_publication_witness("calendar", forged) is None
    snapshot = calendar_publish(env)
    witness = scheduler.prepare_behavior_source_publication_witness("calendar", deepcopy(snapshot))
    assert type(witness) is Witness and witness.current()
    for source in ("reminders", "email", None):
        assert scheduler.prepare_behavior_source_publication_witness(source, snapshot) is None
    changed = deepcopy(snapshot)
    changed["diagnostics"]["snapshot_started_at"] -= 1
    assert scheduler.prepare_behavior_source_publication_witness("calendar", changed) is None
    again = scheduler.connectors_status()["calendar"]
    again["diagnostics"]["authorized"] = False
    assert witness.current()
    assert scheduler.connectors_status()["calendar"]["diagnostics"]["authorized"] is True


@pytest.mark.parametrize("changes", [dict(authorized=False), dict(authorized=None), dict(syncing=True),
    dict(complete=False), dict(complete="yes"), dict(partial=True), dict(snapshot_started_at=None),
    dict(snapshot_started_at=float("nan")), dict(snapshot_started_at=1)])
def test_calendar_unknown_failed_partial_and_expired_metadata_are_unready(env, changes):
    snapshot = calendar_publish(env, **changes)
    assert scheduler.prepare_behavior_source_publication_witness("calendar", snapshot) is None


def test_calendar_input_alias_cannot_mutate_authority(env):
    diagnostic = env[4]()
    scheduler.record_sync("calendar", 0, diagnostic)
    witness = scheduler.prepare_behavior_source_publication_witness("calendar", scheduler.connectors_status()["calendar"])
    diagnostic["authorized"] = False
    assert witness.current()
    assert scheduler.connectors_status()["calendar"]["diagnostics"]["authorized"] is True


def test_reminders_restored_sql_or_arbitrary_ready_dto_cannot_mint_permission(env):
    store = env[2]()
    payload = dict(available=True, syncing=False, last_sync=env[0][0]-1,
                  snapshot_started_at=env[0][0]-1, diagnostics=dict(complete=True, partial=False))
    with store.transaction() as db:
        db.execute("INSERT INTO today_source_sync VALUES ('reminders', ?)", (json.dumps(payload),))
    assert store.prepare_behavior_source_publication_witness("reminders", env[5](store)) is None
    another = env[2](store.path)
    assert another.prepare_behavior_source_publication_witness("reminders", payload) is None
    env[3](2)
    snapshot = reminders_publish(env, store)
    witness = store.prepare_behavior_source_publication_witness("reminders", deepcopy(snapshot))
    assert witness.current()
    changed = dict(snapshot, snapshot_started_at=snapshot["snapshot_started_at"]-1)
    assert store.prepare_behavior_source_publication_witness("reminders", changed) is None
    assert store.prepare_behavior_source_publication_witness("calendar", snapshot) is None
    assert store.prepare_behavior_source_publication_witness("reminders", env[5](store)).current()
    assert witness.current()  # Getter and preparation do not withdraw or renew.


@pytest.mark.parametrize("changes", [dict(authorized=False), dict(authorized=None), dict(available=False), dict(syncing=True),
    dict(complete=False), dict(partial=True), dict(snapshot_started_at=None)])
def test_reminders_receipt_preserves_legacy_getter_but_unknown_permission_stays_unready(env, changes):
    store = env[2]()
    snapshot = reminders_publish(env, store, **changes)
    assert snapshot
    assert store.prepare_behavior_source_publication_witness("reminders", snapshot) is None


@pytest.mark.parametrize("source", ["calendar", "reminders"])
def test_native_receipt_original_expiry_and_clock_rollback_cannot_renew_permission(env, source):
    store = env[2]() if source == "reminders" else None
    get = (lambda: env[5](store)) if store else (lambda: scheduler.connectors_status()["calendar"])
    prepare = store.prepare_behavior_source_publication_witness if store else scheduler.prepare_behavior_source_publication_witness
    snapshot = reminders_publish(env, store) if store else calendar_publish(env)
    first = prepare(source, snapshot)
    env[3](1198)
    later = prepare(source, get())
    assert first.current() and later.current()
    env[3](1)
    assert not first.current() and not later.current() and prepare(source, get()) is None
    env[1].observe(env[0][0]-1, env[0][1]-1)
    env[1].observe(env[0][0]+1, env[0][1]+1)
    assert not first.current()


@pytest.mark.parametrize("source", ["calendar", "reminders"])
def test_reentrant_receipt_change_refused_before_any_memory_or_sql_mutation(env, source):
    store = env[2]() if source == "reminders" else None
    snapshot = reminders_publish(env, store) if store else calendar_publish(env)
    prepare = store.prepare_behavior_source_publication_witness if store else scheduler.prepare_behavior_source_publication_witness
    witness = prepare(source, snapshot)
    with publication(witness):
        with pytest.raises(PublicationBusy, match="not applied"):
            if store:
                store.today_source_unavailable("reminders", env[4](snapshot_started_at=env[0][0]))
            else:
                scheduler.record_sync("calendar", 0, env[4](authorized=False))
        assert witness.current()
    assert (env[5](store) if store else scheduler.connectors_status()["calendar"]) == snapshot


def test_nested_native_receipt_arms_only_after_successful_outer_commit(env):
    store = env[2]()
    snapshot = reminders_publish(env, store)
    old = store.prepare_behavior_source_publication_witness("reminders", snapshot)
    env[3](2)
    observer = sqlite3.connect(str(store.path))
    try:
        with store.transaction() as db:
            store.sync_source("reminders", [], diagnostics=env[4]())
            assert not old.current()
            assert store.prepare_behavior_source_publication_witness("reminders", snapshot) is None
            current = json.loads(observer.execute("SELECT payload FROM today_source_sync WHERE source='reminders'").fetchone()[0])
            assert current == snapshot  # Independent connection sees only prior durable receipt.
            db.execute("INSERT INTO today_preferences VALUES (?,?,?,?)", ("2030-01-01", "UTC", "{}", 1))
        assert store.prepare_behavior_source_publication_witness("reminders", env[5](store)).current()
        assert store._db.execute("SELECT count(*) FROM today_preferences").fetchone()[0] == 1
    finally:
        observer.close()


def test_outer_and_nested_receipt_rollback_never_publish_uncommitted_authority(env):
    store = env[2]()
    original = reminders_publish(env, store)
    old = store.prepare_behavior_source_publication_witness("reminders", original)
    env[3](2)
    with pytest.raises(RuntimeError, match="outer failure"):
        with store.transaction():
            store.sync_source("reminders", [], diagnostics=env[4]())
            raise RuntimeError("outer failure")
    assert env[5](store) == original and not old.current()
    assert store.prepare_behavior_source_publication_witness("reminders", original) is None
    env[3](2)
    with store.transaction() as db:
        db.execute("INSERT INTO today_preferences VALUES (?,?,?,?)", ("2030-01-01", "UTC", "{}", 1))
        with pytest.raises(RuntimeError, match="nested failure"):
            with store.transaction():
                store.sync_source("reminders", [], diagnostics=env[4]())
                raise RuntimeError("nested failure")
    assert env[5](store) == original
    assert store.prepare_behavior_source_publication_witness("reminders", original) is None
    assert store._db.execute("SELECT count(*) FROM today_preferences").fetchone()[0] == 1


def test_receipt_failure_rolls_back_database_and_does_not_revive_previous_permission(env):
    store = env[2]()
    original = reminders_publish(env, store)
    old = store.prepare_behavior_source_publication_witness("reminders", original)
    env[3](2)
    with pytest.raises((ValueError, sqlite3.Error)):
        store.sync_source("reminders", [dict(source_id="bad", title={"unsupported": "SQL value"}, when_ts=env[0][0])], diagnostics=env[4]())
    assert not old.current() and env[5](store) == original
    assert store.prepare_behavior_source_publication_witness("reminders", original) is None
    snapshot = reminders_publish(env, store)
    assert store.prepare_behavior_source_publication_witness("reminders", snapshot).current()


def test_another_connection_and_path_alias_withdraw_shared_receipt_head(env, tmp_path):
    store = env[2]()
    alias = tmp_path / "alias.db"
    alias.symlink_to(store.path)
    another = env[2](alias)
    original = reminders_publish(env, store)
    old = store.prepare_behavior_source_publication_witness("reminders", original)
    assert another.prepare_behavior_source_publication_witness("reminders", original).current()
    env[3](2)
    another.today_source_unavailable("reminders", env[4]())
    assert not old.current()
    assert store.prepare_behavior_source_publication_witness("reminders", env[5](store)) is None


@pytest.mark.parametrize("source", ["calendar", "reminders"])
def test_receipt_revoker_waits_until_publisher_finishes(env, source):
    store = env[2]() if source == "reminders" else None
    snapshot = reminders_publish(env, store) if store else calendar_publish(env)
    prepare = store.prepare_behavior_source_publication_witness if store else scheduler.prepare_behavior_source_publication_witness
    witness = prepare(source, snapshot)
    attempted, done = threading.Event(), threading.Event()
    def revoke():
        attempted.set()
        if store:
            store.today_source_unavailable("reminders", env[4](snapshot_started_at=env[0][0]))
        else:
            scheduler.record_sync("calendar", 0, env[4](authorized=False))
        done.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with publication(witness) as lease:
            task = pool.submit(revoke)
            assert attempted.wait(2) and not done.is_set() and lease.current()
        task.result(timeout=3)
    assert done.is_set() and not witness.current()


@pytest.mark.parametrize("source", ["calendar", "reminders"])
def test_witness_has_no_sql_getter_or_lock_callbacks(env, source, monkeypatch):
    store = env[2]() if source == "reminders" else None
    snapshot = reminders_publish(env, store) if store else calendar_publish(env)
    prepare = store.prepare_behavior_source_publication_witness if store else scheduler.prepare_behavior_source_publication_witness
    witness = prepare(source, snapshot)
    def denied(*_a, **_k):
        raise AssertionError("Memory witness called metadata getter or SQL")
    monkeypatch.setattr(scheduler, "connectors_status", denied)
    if store:
        store._db.set_authorizer(denied)
        monkeypatch.setattr(store, "today_snapshot", denied)
    with publication(witness) as lease:
        assert lease.current() and witness.current()
    if store:
        store._db.set_authorizer(None)


@pytest.mark.parametrize("source", ["calendar", "reminders"])
def test_fresh_permission_clock_rollback_is_latched_before_its_expiry(env, source):
    store = env[2]() if source == "reminders" else None
    snapshot = reminders_publish(env, store) if store else calendar_publish(env)
    prepare = store.prepare_behavior_source_publication_witness if store else scheduler.prepare_behavior_source_publication_witness
    witness = prepare(source, snapshot)
    assert witness.current()
    env[1].observe(env[0][0]-1, env[0][1])
    assert not witness.current()
    env[1].observe(env[0][0]+1, env[0][1]+1)
    assert not witness.current()


def test_unrelated_content_transaction_does_not_replace_receipt_authority(env, tmp_path):
    store = env[2]()
    snapshot = reminders_publish(env, store)
    witness = store.prepare_behavior_source_publication_witness("reminders", snapshot)
    with store.transaction() as db:
        db.execute("INSERT INTO today_preferences VALUES (?,?,?,?)", ("2030-01-01", "UTC", "{}", 1))
    assert witness.current()
    other = env[2](tmp_path / "other.db")
    assert other._behavior_receipt_head is not store._behavior_receipt_head
    assert other.prepare_behavior_source_publication_witness("reminders", snapshot) is None


def test_caller_owned_raw_transaction_keeps_commit_control_and_cannot_arm_receipt(env):
    store = env[2]()
    store._db.execute("BEGIN IMMEDIATE")
    store.sync_source("reminders", [], diagnostics=env[4]())
    assert store._db.in_transaction
    store._db.commit()  # Outside managed transaction, no process-owned completion grant.
    assert store.prepare_behavior_source_publication_witness("reminders", env[5](store)) is None
