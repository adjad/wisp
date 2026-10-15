"""Application publication ordering; no source, model, native or real data."""
from concurrent.futures import ThreadPoolExecutor
import pickle
import sqlite3
import threading

import pytest

from service.publication_authority import (
    Authority, Clock, PublicationBusy, PublicationUnavailable, Witness,
    publication, revocation,
)


def ready():
    authority = Authority("synthetic-source")
    authority.publish()
    return authority,Witness.combine(authority.token())


def test_reentrant_revocation_is_not_applied_or_falsely_acknowledged():
    authority,witness = ready()
    with publication(witness) as lease:
        with pytest.raises(PublicationBusy,match="not applied"):
            authority.withdraw()
        assert lease.current() and witness.current()
    assert not lease.current()
    authority.withdraw()
    assert not witness.current()


def test_publisher_wins_before_a_separate_thread_can_acknowledge_revocation():
    authority,witness = ready()
    attempted,acknowledged = threading.Event(),threading.Event()
    def revoke():
        attempted.set()
        authority.withdraw()
        acknowledged.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with publication(witness) as lease:
            task = pool.submit(revoke)
            assert attempted.wait(3) and not acknowledged.is_set()
            assert lease.current()
        task.result(timeout=3)
    assert acknowledged.is_set() and not witness.current()


def test_revocation_wins_and_a_waiting_publication_cannot_start():
    authority,witness = ready()
    attempted = threading.Event()
    def publish():
        attempted.set()
        with pytest.raises(PublicationUnavailable):
            with publication(witness):
                raise AssertionError("Revoked publication ran")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with revocation():
            authority.withdraw()
            task = pool.submit(publish)
            assert attempted.wait(3)
        task.result(timeout=3)


def test_nested_mutation_is_allowed_but_cannot_publish_inside_a_revoker():
    authority,witness = ready()
    with revocation(),revocation():
        with pytest.raises(PublicationBusy):
            with publication(witness):
                raise AssertionError("Published inside mutation")
        authority.withdraw()
    assert not witness.current()


def test_tokens_are_process_local_and_missing_evidence_cannot_mint_a_witness():
    authority = Authority("synthetic-source")
    assert authority.token() is None and Witness.combine(authority.token()) is None
    authority.publish()
    token = authority.token()
    with pytest.raises(TypeError):
        pickle.dumps(token)
    with pytest.raises(TypeError):
        Witness(({},))
    with pytest.raises(PublicationUnavailable):
        with publication(None):
            raise AssertionError("Missing witness accepted")


def test_expiry_and_clock_rollback_do_not_renew_tokens():
    authority,_ = ready()
    clock = Clock(live=False)
    clock.observe(100,50)
    witness = Witness.combine(authority.token(clock=clock,expires_wall=110,expires_monotonic=60))
    clock.observe(105,55)
    assert witness.current()
    clock.observe(99,55)
    assert not witness.current()
    clock.observe(105,49)
    assert not witness.current()
    clock.observe(110,60)
    assert not witness.current()
    clock.observe(106,56)
    assert not witness.current()  # A recovered reading cannot rearm the same clock epoch.


def test_builtin_clock_observations_latch_rollback_without_rearming(monkeypatch):
    from service import publication_authority as module
    reading = [100,50]
    monkeypatch.setattr(module.time,"time",lambda:reading[0])
    monkeypatch.setattr(module.time,"monotonic",lambda:reading[1])
    clock = Clock()
    authority,_ = ready()
    witness = Witness.combine(authority.token(clock=clock,expires_wall=110,expires_monotonic=60))
    reading[:] = [105,55]
    assert witness.current()
    reading[:] = [104,56]
    assert not witness.current()
    reading[:] = [106,57]
    assert not witness.current()


def test_source_owner_drop_invalidates_a_memory_stamp_without_a_getter():
    import gc
    class Owner:
        pass
    owner = Owner()
    authority = Authority("synthetic-owned-source",owner=owner)
    authority.publish()
    witness = Witness.combine(authority.token())
    assert witness.current()
    del owner
    gc.collect()
    assert not witness.current()


def test_memory_witness_check_never_calls_config_or_sql_or_file_io(monkeypatch):
    from service import config
    authority,witness = ready()
    def forbidden(*args,**kwargs):
        raise AssertionError("Witness attempted I/O or configuration resolution")
    monkeypatch.setattr(config,"models_config",forbidden)
    monkeypatch.setattr(sqlite3,"connect",forbidden)
    monkeypatch.setattr("builtins.open",forbidden)
    assert witness.current()
    with publication(witness) as lease:
        assert lease.current()


def test_real_config_revocation_is_refused_before_overlay_or_authority_change(tmp_path,monkeypatch):
    from service import config
    from service.config import authority_changes
    monkeypatch.setattr(config,"USER_CONFIG",tmp_path/"config.yaml")
    config.models_config.cache_clear()
    config.models_config()
    before = authority_changes.current()
    witness = Witness.combine(authority_changes.publication_token(before))
    assert witness is not None and witness.current()
    with publication(witness):
        with pytest.raises(PublicationBusy):
            config.set_behavior_enabled(False)
        assert authority_changes.current() is before and not config.USER_CONFIG.exists()
    config.set_behavior_enabled(False)
    assert config.USER_CONFIG.exists() and not witness.current()
    assert config.get_behavior_enabled() is False
    config.models_config.cache_clear()


def test_direct_engine_and_activity_settings_changes_withdraw_the_same_memory_stamp(tmp_path,monkeypatch):
    from service.behavior.engine import BehaviorEngine
    engine = BehaviorEngine(tmp_path/"settings.db",clock=lambda:100,tz="UTC")
    try:
        engine.configure_signals({"email":True,"interpretation":True},100)
        before = engine.signal_settings()
        witness = Witness.combine(engine.prepare_settings_publication_token(before))
        assert witness.current()
        with publication(witness):
            with pytest.raises(PublicationBusy):
                engine.configure_signals({"interpretation":False},101)
            with pytest.raises(PublicationBusy):
                engine._activity.configure({"interpretation":False},101)
            def forbidden(*args,**kwargs):
                raise AssertionError("Memory witness attempted settings SQL")
            with monkeypatch.context() as pure:
                pure.setattr(engine,"signal_settings",forbidden)
                pure.setattr(engine._store,"meta_get",forbidden)
                assert witness.current()
        assert engine.signal_settings() == before
        engine._activity.configure({"interpretation":False},101)
        assert not witness.current() and engine.prepare_settings_publication_token(before) is None
        current = Witness.combine(engine.prepare_settings_publication_token(engine.signal_settings()))
        assert current.current()
        engine.close()
        assert not current.current()
    finally:
        engine.close()


def test_cancellation_uses_a_known_event_flag_without_an_arbitrary_callback(monkeypatch):
    authority,_ = ready()
    event = threading.Event()
    witness = Witness.combine(authority.token(cancelled_by=event))
    with pytest.raises(TypeError):
        authority.token(cancelled_by={"cancelled":False})
    def forbidden():
        raise AssertionError("Cancellation callback ran inside the pure witness")
    monkeypatch.setattr(event,"is_set",forbidden)
    with publication(witness):
        assert witness.current()
    with revocation():
        event.set()
    assert not witness.current()


def test_retained_engine_content_stamp_is_exact_pure_and_withdrawn_before_replacement(tmp_path,monkeypatch):
    from service.behavior.engine import BehaviorEngine
    from service.behavior.types import Observation
    engine = BehaviorEngine(tmp_path/"retained.db",clock=lambda:100,tz="UTC")
    try:
        engine.configure_signals({"calendar_context":True},100)
        row = Observation("calendar-one","event",200,end_ts=260,subject="Synthetic plan",source="calendar",source_id="one")
        engine.observe_snapshot("calendar",[row],window_start=100,window_end=300,taken_at=100)
        context = engine.semantic_history("calendar",100,expected_sync=100,metadata=True)
        witness = Witness.combine(engine.prepare_semantic_publication_token("calendar",context,100,expected_sync=100))
        assert witness.current()
        with publication(witness):
            with pytest.raises(PublicationBusy):
                engine.observe([row])
            with pytest.raises(PublicationBusy):
                engine.history_control("calendar","restart")
            with pytest.raises(PublicationBusy):
                engine._activity.ingest({},100)
            def forbidden(*args,**kwargs):
                raise AssertionError("Content witness queried semantic SQL")
            with monkeypatch.context() as pure:
                pure.setattr(engine,"semantic_history",forbidden)
                pure.setattr(engine._store,"observations",forbidden)
                assert witness.current()
        from dataclasses import replace
        engine.observe_snapshot("calendar",[replace(row,subject="Changed plan")],window_start=100,window_end=300,taken_at=101)
        assert not witness.current()
        assert engine.prepare_semantic_publication_token("calendar",context,101,expected_sync=100) is None
    finally:
        engine.close()


def test_direct_activity_ingest_invalidates_prepared_routines_even_without_engine_wrapper(tmp_path):
    from service.behavior.engine import BehaviorEngine
    from tests.test_behavior_availability import history,NOW
    from datetime import datetime,timedelta,timezone
    engine = BehaviorEngine(tmp_path/"activity-data.db",clock=lambda:NOW,tz="UTC")
    try:
        engine.configure_signals({"activity":True},NOW-100)
        engine.observe(history())
        first = datetime.fromtimestamp(NOW,timezone.utc).date()
        patterns = [p for offset in range(7) for p in engine.availability_patterns(
            (first+timedelta(days=offset)).isoformat(),"UTC",now=NOW)]
        assert patterns
        witness = engine.prepare_availability_publication_witness(patterns,first.isoformat(),"UTC",(),NOW)
        assert witness.current()
        lease = engine.activity_lease("collector-session-1234",NOW-60)
        result = engine._activity.ingest(dict(lease=lease,intervals=[dict(start=NOW-45,end=NOW-30,
            idle_seconds=0,sequence=1,bundle="test.App",timezone="UTC",screen_awake=True,session_active=True)]),NOW)
        assert result["accepted"] == 1 and not witness.current()
    finally:
        engine.close()
