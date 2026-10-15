"""Synthetic native-cache publication stamps; no app/native scan/model or network."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib
import threading
from types import MappingProxyType

import pytest

from service.publication_authority import Authority, Clock, PublicationBusy, Witness, publication
from tests.test_message_content import record, v3_carrier


@pytest.fixture
def sources(monkeypatch, tmp_path):
    from service.tools import cache_store
    saved = {}
    monkeypatch.setattr(cache_store, "load", lambda name: saved.get(name, ""))
    monkeypatch.setattr(cache_store, "save", lambda name, value: saved.__setitem__(name, value))
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    from service.tools import notes_tools, imessage_tools, browser_history_tools
    modules = dict(notes=notes_tools, messages=imessage_tools, browser=browser_history_tools)
    # Reinitialize only these source modules against the fake empty cache.
    for module in modules.values():
        importlib.reload(module)
    now = [1800000000.0, 10000.0]
    monkeypatch.setattr(notes_tools.time, "time", lambda: now[0])
    monkeypatch.setattr(notes_tools.time, "monotonic", lambda: now[1])
    clocks = []
    for name, module in modules.items():
        clock = Clock(live=False)
        clock.observe(*now)
        monkeypatch.setattr(module, "LIVE_CLOCK", clock)
        monkeypatch.setattr(module, "_publication_authority", Authority("test-"+name))
        clocks.append(clock)
    def advance(seconds):
        now[0] += seconds
        now[1] += seconds
        for clock in clocks:
            clock.observe(*now)
    def publish(name, *, available=True):
        if name == "notes":
            notes_tools.cache_notes(f"{now[0]-10}\x01Synthetic founder plan\x01Work\x01Prepare pilot brief", available,
                                    snapshot_started_at=now[0]-1)
        elif name == "messages":
            imessage_tools.cache_messages(v3_carrier([record()]), available)
        else:
            browser_history_tools.apply_browser_history_sync(dict(revision=browser_history_tools._revision+1,
                enabled=available, browsers={b: dict(lines=f"{now[0]-10} | founder.test | /pilot | Pilot",
                    diagnostics=dict(available=True)) for b in ("safari", "chrome")}))
    getters = dict(notes=notes_tools.raw_notes_snapshot,
                   messages=imessage_tools.behavior_messages_snapshot_with_receipt,
                   browser=browser_history_tools.current_browser_snapshot)
    return modules, now, advance, publish, getters


@pytest.mark.parametrize("name", ["notes", "messages", "browser"])
def test_issued_positive_capture_and_reads_do_not_replace_prepared_witness(sources, name):
    modules, _, _, publish, getters = sources
    module = modules[name]
    publish(name)
    snapshot = getters[name]()
    witness = module.prepare_publication_witness(snapshot)
    assert type(witness) is Witness and witness.current()
    again = getters[name]()
    assert again is not snapshot
    assert witness.current()  # Same source bytes/head; no read-induced revocation.
    assert module.prepare_publication_witness(again).current()
    assert module.prepare_publication_witness(snapshot) is None  # Bounded one-capture slot.
    with publication(witness) as lease:
        assert lease.current()


@pytest.mark.parametrize("name", ["notes", "messages", "browser"])
def test_restored_unknown_and_copied_snapshots_cannot_restore_authority(sources, name):
    modules, _, _, publish, getters = sources
    module = modules[name]
    assert module.prepare_publication_witness(None) is None
    assert module.prepare_publication_witness({}) is None
    publish(name)
    snapshot = getters[name]()
    clone = replace(snapshot) if name == "notes" else MappingProxyType(dict(snapshot))
    assert clone == snapshot and clone is not snapshot
    assert module.prepare_publication_witness(clone) is None
    if name == "notes":
        module._notes_origin = "restored"
        assert module.prepare_publication_witness(getters[name]()) is None


@pytest.mark.parametrize("name", ["notes", "messages", "browser"])
def test_new_publication_and_unavailable_revoke_old_memory_head(sources, name):
    modules, _, _, publish, getters = sources
    module = modules[name]
    publish(name)
    original = getters[name]()
    first = module.prepare_publication_witness(original)
    publish(name)  # Identical contents still replace the source receipt/revision.
    assert not first.current() and module.prepare_publication_witness(original) is None
    current = module.prepare_publication_witness(getters[name]())
    assert current.current()
    publish(name, available=False)
    assert not current.current()
    snapshot = getters[name]()
    assert module.prepare_publication_witness(snapshot) is None


@pytest.mark.parametrize("name,ttl", [("notes", 86400), ("messages", 600), ("browser", 10800)])
def test_original_expiry_is_not_renewed_by_getter_or_preparation(sources, name, ttl):
    modules, _, advance, publish, getters = sources
    module = modules[name]
    publish(name)
    first = module.prepare_publication_witness(getters[name]())
    advance(ttl-2)
    later = module.prepare_publication_witness(getters[name]())
    assert first.current() and later.current()
    advance(2)
    assert not first.current() and not later.current()
    assert module.prepare_publication_witness(getters[name]()) is None


@pytest.mark.parametrize("name", ["notes", "messages", "browser"])
def test_reentrant_withdrawal_refused_before_native_state_change(sources, name):
    modules, _, _, publish, getters = sources
    module = modules[name]
    publish(name)
    capture = getters[name]()
    witness = module.prepare_publication_witness(capture)
    with publication(witness) as lease:
        with pytest.raises(PublicationBusy, match="not applied"):
            publish(name, available=False)
        assert lease.current()
    # The refused operation changed no publication, bytes, availability or receipt.
    assert getters[name]() == capture and witness.current()
    publish(name, available=False)
    assert not witness.current()


@pytest.mark.parametrize("name", ["notes", "messages", "browser"])
def test_other_thread_revocation_cannot_acknowledge_before_publication_completes(sources, name):
    modules, _, _, publish, getters = sources
    publish(name)
    witness = modules[name].prepare_publication_witness(getters[name]())
    attempted, acknowledged = threading.Event(), threading.Event()
    def revoke():
        attempted.set()
        publish(name, available=False)
        acknowledged.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with publication(witness) as lease:
            task = pool.submit(revoke)
            assert attempted.wait(2) and not acknowledged.is_set()
            assert lease.current()
        task.result(timeout=3)
    assert acknowledged.is_set() and not witness.current()


@pytest.mark.parametrize("name", ["notes", "messages", "browser"])
def test_witness_check_is_memory_only_and_has_no_source_getter_or_lock(sources, name, monkeypatch):
    modules, _, _, publish, getters = sources
    publish(name)
    witness = modules[name].prepare_publication_witness(getters[name]())
    def forbidden(*_a, **_k):
        raise AssertionError("I/O or source getter called by memory witness")
    for module in modules.values():
        for method in ("raw_notes_snapshot", "behavior_messages_snapshot_with_receipt", "current_browser_snapshot"):
            if hasattr(module, method):
                monkeypatch.setattr(module, method, forbidden)
    from service.tools import cache_store
    monkeypatch.setattr(cache_store, "load", forbidden)
    monkeypatch.setattr(cache_store, "save", forbidden)
    assert witness.current()
    with publication(witness) as lease:
        assert lease.current()


def test_notes_requires_matching_latest_live_receipt_and_valid_carrier(sources):
    modules, now, _, _, _ = sources
    notes = modules["notes"]
    for raw, started in [("broken carrier", now[0]-1), ("1\x01Title\x01Work\x01Body", None)]:
        notes.cache_notes(raw, True, snapshot_started_at=started)
        assert notes.prepare_publication_witness(notes.raw_notes_snapshot()) is None
    notes.cache_notes("1\x01Title\x01Work\x01Body", True, snapshot_started_at=now[0]-1)
    assert notes.prepare_publication_witness(notes.raw_notes_snapshot()).current()
    notes.cache_notes("", False, snapshot_started_at=now[0])
    assert notes.raw_notes_snapshot().records  # Existing held-body getter behavior.
    assert notes.prepare_publication_witness(notes.raw_notes_snapshot()) is None


def test_messages_keeps_partial_complete_record_subset_and_rejects_bad_coverage(sources):
    modules, _, _, _, _ = sources
    messages = modules["messages"]
    messages.cache_messages(v3_carrier([record(guid="one"), record(guid="two")], partial=(1,)), True)
    snapshot = messages.behavior_messages_snapshot_with_receipt()
    assert len(snapshot["records"]) == 1 and snapshot["coverage"]["status"] == "partial"
    assert messages.structured_messages_snapshot_with_receipt() is None
    assert messages.prepare_publication_witness(snapshot).current()
    messages.cache_messages(v3_carrier([record()], coverage=dict(emitted=0)), True)
    assert messages.behavior_messages_snapshot_with_receipt() is None


def test_browser_expiring_one_capture_member_cannot_be_omitted_from_original_bounds(sources):
    modules, now, advance, publish, getters = sources
    browser = modules["browser"]
    publish("browser")
    advance(100)
    browser.cache_browser_history("chrome", f"{now[0]} | new.test | / | New", True)
    snapshot = getters["browser"]()
    advance(10701)
    assert browser.prepare_publication_witness(snapshot) is None
    fresh = getters["browser"]()
    assert {row["host"] for row in fresh["rows"]} == {"new.test"}
    assert browser.prepare_publication_witness(fresh).current()
    browser._purge_if_expired()
    assert browser.prepare_publication_witness(getters["browser"]()).current()


@pytest.mark.parametrize("name", ["notes", "messages", "browser"])
def test_persistence_failure_stays_unready_and_withdraws_previous_stamp(sources, name, monkeypatch):
    modules, _, _, publish, getters = sources
    from service.tools import cache_store
    publish(name)
    witness = modules[name].prepare_publication_witness(getters[name]())
    def fail(*_a, **_k):
        raise OSError("Synthetic cache failure")
    monkeypatch.setattr(cache_store, "save", fail)
    with pytest.raises(OSError, match="Synthetic"):
        publish(name)
    assert not witness.current()
    assert modules[name].prepare_publication_witness(getters[name]()) is None


@pytest.mark.parametrize("name", ["notes", "messages", "browser"])
def test_pending_publication_is_unready_during_persistence_and_native_locks_are_released(sources, name, monkeypatch):
    modules, _, _, publish, getters = sources
    module = modules[name]
    from service.tools import cache_store
    publish(name)
    old = module.prepare_publication_witness(getters[name]())
    saved = cache_store.save
    observed = []
    def persist(key, value):
        assert not old.current()
        native_lock = getattr(module, "_publication_lock", getattr(module, "_snapshot_lock", None))
        if native_lock is not None:
            assert not native_lock._is_owned()
        capture = getters[name]()
        assert module.prepare_publication_witness(capture) is None
        observed.append(key)
        saved(key, value)
    monkeypatch.setattr(cache_store, "save", persist)
    publish(name)
    assert observed and module.prepare_publication_witness(getters[name]()).current()


@pytest.mark.parametrize("name", ["notes", "browser"])
def test_purge_and_direct_browser_off_refuse_reentry_before_mutation(sources, name):
    modules, _, _, publish, getters = sources
    module = modules[name]
    publish(name)
    witness = module.prepare_publication_witness(getters[name]())
    with publication(witness):
        with pytest.raises(PublicationBusy):
            module._purge_if_expired()
        if name == "browser":
            with pytest.raises(PublicationBusy):
                module.set_browser_history_enabled(False)
    assert witness.current()


def test_browser_stale_request_does_not_revoke_a_current_publication(sources):
    modules, _, _, publish, getters = sources
    browser = modules["browser"]
    publish("browser")
    witness = browser.prepare_publication_witness(getters["browser"]())
    assert browser.apply_browser_history_sync(dict(revision=browser._revision, enabled=False)) is False
    assert witness.current()


@pytest.mark.parametrize("name", ["notes", "messages", "browser"])
def test_known_clock_rollback_cannot_rearm_a_native_witness(sources, name):
    modules, now, _, publish, getters = sources
    publish(name)
    witness = modules[name].prepare_publication_witness(getters[name]())
    clock = modules[name].LIVE_CLOCK
    clock.observe(now[0]-1, now[1])
    assert not witness.current()
    clock.observe(now[0]+1, now[1]+1)
    assert not witness.current()
