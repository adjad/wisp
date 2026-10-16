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


def test_shared_database_settings_and_content_heads_cross_aliases_but_close_is_local(tmp_path):
    from service.behavior.engine import BehaviorEngine
    from service.behavior.types import Observation
    path = tmp_path/"shared.db"
    first = BehaviorEngine(path,clock=lambda:100,tz="UTC")
    alias = tmp_path/"alias.db"
    alias.symlink_to(path)
    second = BehaviorEngine(alias,clock=lambda:100,tz="UTC")
    other = BehaviorEngine(tmp_path/"other.db",clock=lambda:100,tz="UTC")
    try:
        first.configure_signals({"email":True,"calendar_context":True},100)
        original = first.signal_settings()
        settings = first.prepare_settings_publication_token(original)
        other_settings = other.prepare_settings_publication_token(other.signal_settings())
        second.configure_signals({"email":False},101)
        assert not settings.current() and other_settings.current()
        row = Observation("shared-event","event",200,end_ts=260,subject="Original",source="calendar",source_id="one")
        first.observe_snapshot("calendar",[row],window_start=100,window_end=300,taken_at=100)
        context = first.semantic_history("calendar",100,expected_sync=100,metadata=True)
        content = first.prepare_semantic_publication_token("calendar",context,100,expected_sync=100)
        from dataclasses import replace
        second.observe([replace(row,subject="Updated through another connection")])
        assert not content.current()
        current = second.semantic_history("calendar",100,expected_sync=100,metadata=True)
        first_content = first.prepare_semantic_publication_token("calendar",current,100,expected_sync=100)
        second_content = second.prepare_semantic_publication_token("calendar",current,100,expected_sync=100)
        second_settings = second.prepare_settings_publication_token(second.signal_settings())
        assert first_content.current() and second_content.current()
        first.close()
        assert not first_content.current() and second_content.current() and second_settings.current()
        assert second.prepare_semantic_publication_token("calendar",current,100,expected_sync=100).current()
        assert other_settings.current()
    finally:
        first.close();second.close();other.close()


def test_content_preparation_cannot_bind_new_head_to_bytes_read_before_another_writer(tmp_path,monkeypatch):
    from service.behavior.engine import BehaviorEngine
    from service.behavior.types import Observation
    first = BehaviorEngine(tmp_path/"race.db",clock=lambda:100,tz="UTC")
    second = BehaviorEngine(tmp_path/"race.db",clock=lambda:100,tz="UTC")
    try:
        first.configure_signals({"calendar_context":True},100)
        row = Observation("event","event",200,end_ts=260,subject="Original",source="calendar",source_id="one")
        first.observe_snapshot("calendar",[row],window_start=100,window_end=300,taken_at=100)
        context = first.semantic_history("calendar",100,expected_sync=100,metadata=True)
        read = first.semantic_history
        def changed_after_read(*args,**kwargs):
            result = read(*args,**kwargs)
            from dataclasses import replace
            second.observe([replace(row,subject="Changed after the full read")])
            return result
        monkeypatch.setattr(first,"semantic_history",changed_after_read)
        assert first.prepare_semantic_publication_token("calendar",context,100,expected_sync=100) is None
    finally:
        first.close();second.close()


def test_retention_withdraws_live_proofs_without_constructing_an_engine_or_runtime(tmp_path,monkeypatch):
    from service.assistant import behavior_runtime as runtime_module
    from service.behavior import engine as engine_module
    from service.behavior.types import Observation
    from service.behavior.source_index import SourceIndex
    now = 1792051200.0
    engine = engine_module.BehaviorEngine(tmp_path/runtime_module.ENGINE_DB,clock=lambda:now,tz="UTC")
    try:
        engine.configure_signals({"calendar_context":True},now)
        row = Observation("old-event","event",now-86400,end_ts=now-86300,subject="Old plan",source="calendar",source_id="one")
        engine.observe_snapshot("calendar",[row],window_start=now-2*86400,window_end=now,taken_at=now)
        context = engine.semantic_history("calendar",now,expected_sync=now,metadata=True)
        content = engine.prepare_semantic_publication_token("calendar",context,now,expected_sync=now)
        settings = engine.prepare_settings_publication_token(engine.signal_settings())
        index = SourceIndex(engine._store)
        index_token = index.publication_token()
        def forbidden(*args,**kwargs):
            raise AssertionError("Retention tried to construct an engine/runtime")
        with monkeypatch.context() as closed:
            closed.setattr(runtime_module,"BehaviorRuntime",forbidden)
            closed.setattr(engine_module,"BehaviorEngine",forbidden)
            result = runtime_module.age_out(tmp_path,now+181*86400)
        assert result["observations"] == 1
        assert not content.current() and not settings.current() and not index_token.current()
        assert index.publication_token().current()
        assert engine.prepare_settings_publication_token(engine.signal_settings()).current()
    finally:
        engine.close()


def test_reentrant_forget_stop_close_and_singleton_replacement_leave_state_untouched(tmp_path,monkeypatch):
    from service.assistant import behavior_runtime as module
    from service.assistant.store import AssistantStore
    monkeypatch.setenv("WISP_BEHAVIOR","1")
    monkeypatch.setattr(module,"_runtime",None)
    monkeypatch.setattr(module,"_forgetting",False)
    runtime = module.BehaviorRuntime(tmp_path/"runtime")
    store = AssistantStore(tmp_path/"assistant.db")
    module.set_runtime(runtime)
    witness = Witness.combine(runtime._publication.token())
    try:
        with publication(witness):
            for change in (lambda:module.forget_all(store,runtime.home),runtime.stop,runtime.close,lambda:module.set_runtime(None)):
                with pytest.raises(PublicationBusy):
                    change()
                assert module._runtime is runtime and not module._forgetting and not runtime._stopped
                assert witness.current() and (runtime.home/module.RUNTIME_DB).exists()
        result = module.forget_all(store,runtime.home)
        assert result["files_deleted"] >= 1 and module._runtime is None and not module._forgetting
        assert not witness.current() and not (runtime.home/module.RUNTIME_DB).exists()
    finally:
        runtime.close();store._db.close()


def test_stop_releases_application_barrier_before_waiting_for_the_worker(tmp_path,monkeypatch):
    from service.assistant.behavior_runtime import BehaviorRuntime
    runtime = BehaviorRuntime(tmp_path)
    withdrawn = threading.Event()
    original = runtime._publication.withdraw
    def withdraw():
        original()
        withdrawn.set()
    monkeypatch.setattr(runtime._publication,"withdraw",withdraw)
    runtime._work_lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            stopping = pool.submit(runtime.stop)
            try:
                assert withdrawn.wait(3)
                def probe():
                    with revocation():
                        return True
                assert pool.submit(probe).result(timeout=3) and not stopping.done()
            finally:
                runtime._work_lock.release()
            stopping.result(timeout=3)
    finally:
        runtime.close()
