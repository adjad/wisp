"""Behavior runtime: ingest -> mine -> nudge -> hub, auto actions + Undo,
Calendar snapshot mapping, profile card. Synthetic data only;
every store lives under tmp_path."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest

from service.assistant import behavior_runtime as br
from service.behavior.types import Action, Feedback, Nudge, Observation, Pattern, Prediction, Window

NOW = datetime(2026, 10, 12, 9, 0).timestamp()          # Monday 9:00 local


def at(hour, minute=0, day=0):
    return (datetime(2026, 10, 12) + timedelta(days=day)).replace(hour=hour, minute=minute).timestamp()


# --------------------------------------------------------------------------
# Fakes that honour the engine contract
# --------------------------------------------------------------------------

class FakeEngine:
    def __init__(self, path=None, nudges=()):
        from service.publication_authority import Authority
        self._fixture_authority = Authority("explicit-test-engine")
        self._fixture_authority.publish()
        self._fixture_grants = {}
        self.path = path
        self.obs: dict[str, Observation] = {}
        self.snapshots: dict[str, dict] = {}
        self.snapshot_calls = []
        self.mined = 0
        self.generated = 0
        self.feedbacks: list[Feedback] = []
        self._queued = list(nudges)
        self._nudges: list[Nudge] = []
        self._patterns = {
            "p_gym": Pattern(id="p_gym", kind="habit", subject="fitness",
                             statement="You go to the gym on weekday mornings (6:30-8:00)",
                             window=Window((0, 1, 2, 3, 4), 390, 480), support=14, opportunities=16,
                             confidence=0.72, evidence=("cal:g:1",)),
            "p_sam": Pattern(id="p_sam", kind="contact_rhythm", subject="Sam",
                             statement="You talk to Sam in the afternoons", support=9,
                             opportunities=12, confidence=0.55),
            "p_b2b": Pattern(id="p_b2b", kind="cancel_propensity", subject="back_to_back_last",
                             statement="You usually cancel the last of 3+ back-to-back meetings",
                             support=5, opportunities=7, confidence=0.41),
        }
        self.card = "- Gym weekday mornings 6:30-8:00\n- Talks to Sam in the afternoons"

    def observe(self, observations):
        n = 0
        for o in observations:
            if self.obs.get(o.id) != o:
                n += 1
            self.obs[o.id] = o
        return n

    def observe_snapshot(self, source, observations, *, window_start, window_end, taken_at):
        observations = list(observations)
        self.snapshot_calls.append((source, [o.id for o in observations], window_start, window_end, taken_at))
        prev = self.snapshots.get(source, {})
        now_ids = {o.id: o for o in observations}
        cancelled = 0
        for oid, o in prev.items():
            if oid not in now_ids and o.ts > taken_at:
                cancelled += 1
                self.obs["x:" + oid] = Observation(id="x:" + oid, kind="event_cancelled", ts=taken_at,
                                                   subject=o.subject, source=source, source_id=o.source_id)
        self.snapshots[source] = now_ids
        upserted = self.observe(observations)
        return {"upserted": upserted, "cancelled": cancelled, "moved": 0}

    def mine(self, now=None):
        self.mined += 1
        return list(self._patterns.values())

    def patterns(self, *, status=None, kind=None):
        return [p for p in self._patterns.values()
                if (status is None or p.status == status) and (kind is None or p.kind == kind)]

    def set_pattern_status(self, pattern_id, status):
        if pattern_id not in self._patterns:
            raise KeyError(pattern_id)
        from dataclasses import replace
        self._patterns[pattern_id] = replace(self._patterns[pattern_id], status=status)
        return self._patterns[pattern_id]

    def predict(self, now=None, horizon_s=86400.0):
        now = now or NOW
        return [Prediction(kind="activity", subject="gym", ts=at(6, 30, 1), end_ts=at(8, 0, 1),
                           probability=0.82, because="14 of the last 16 weekday mornings", pattern_id="p_gym"),
                Prediction(kind="cancel_risk", subject="Team sync", ts=at(16, 0, 1), probability=0.6,
                           because="last of 3 back-to-back meetings", pattern_id="p_b2b")]

    def generate_nudges(self, now=None):
        self.generated += 1
        out = []
        for n in self._queued:
            if any(x.id == n.id for x in self._nudges):
                continue
            from dataclasses import replace
            n = replace(n, seq=len(self._nudges) + 1)
            self._nudges.append(n)
            from service.publication_authority import Witness
            self._fixture_grants[n.id] = (json.dumps(n.to_dict(),sort_keys=True,allow_nan=False),
                Witness((self._fixture_authority.token(),)),("wisp",))
            out.append(n)
        self._queued = []
        return out

    def signal_settings(self):
        from service.behavior.activity import DEFAULTS
        return {"settings":dict(DEFAULTS),"epoch":"explicit-test-engine"}

    def prepare_settings_publication_token(self, expected):
        from service.publication_authority import Witness
        return Witness((self._fixture_authority.token(),)) if expected==self.signal_settings() else None

    def queued_nudge_authority(self, nudge, now):
        grant = self._fixture_grants.get(nudge.id)
        if (grant is not None and grant[1].current()
                and grant[0]==json.dumps(nudge.to_dict(),sort_keys=True,allow_nan=False)
                and (nudge.expires_at is None or nudge.expires_at>now)):
            return grant
        return None

    def nudge_seq_space(self):
        return {"store_id": "fake-store", "max_seq": max((n.seq for n in self._nudges), default=0)}

    def nudges_since(self, cursor=0, limit=50):
        rows = [n for n in self._nudges if n.seq > cursor][:limit]
        return rows, (rows[-1].seq if rows else cursor)

    def feedback(self, fb):
        self.feedbacks.append(fb)

    def profile_card(self, now=None, max_bytes=700):
        return self.card

    def describe(self, subject=None, now=None):
        pats = [p.to_dict() for p in self._patterns.values()]
        if subject:
            pats = [p for p in pats if subject.casefold() in (p["subject"] + p["statement"]).casefold()]
        return {"patterns": pats, "predictions": [p.to_dict() for p in self.predict(now)],
                "summary": "Early riser who protects gym time."}

    def status(self):
        kinds = {}
        for o in self.obs.values():
            kinds[o.kind] = kinds.get(o.kind, 0) + 1
        return {"observations": kinds, "patterns": len(self._patterns), "nudge_cursor": len(self._nudges)}


def FakeHub():
    """Legacy fixture name: record the real synchronous Hub/Store delivery."""
    from service.assistant.hub import Hub
    hub = Hub()
    hub.events = []
    hub._fixture_recording = True
    publish = hub.publish_committed_sync
    def record(lease,receipt,runtime,loop):
        event = publish(lease,receipt,runtime,loop)
        if receipt.pending:
            data = json.loads(receipt.plan.data)
            hub.events.append(dict(event=event,dedupe_key=data["key"],durable=True,
                                   expires_at=data["expires_at"]))
            if event.get("auto_performed"):
                hub.events.append(dict(event={"type":"changed"},dedupe_key=None,durable=False,expires_at=None))
        return event
    hub.publish_committed_sync = record
    publish_normal = hub.publish_sync
    def record_normal(event,**kwargs):
        result = publish_normal(event,**kwargs)
        hub.events.append(dict(event=result,dedupe_key=kwargs.get("dedupe_key"),
            durable=kwargs.get("durable",True),expires_at=kwargs.get("expires_at")))
        return result
    hub.publish_sync = record_normal
    return hub


def gym_conflict_nudge(nid="n1", expires=None):
    return Nudge(
        id=nid, kind="habit_conflict", title="Gym vs. 7:00 meeting",
        body="Your 7:00 call lands on gym time.", because="14 of the last 16 weekday mornings",
        created_at=NOW, expires_at=expires if expires is not None else at(7, 0, 1), priority=0.8,
        pattern_ids=("p_gym",),
        actions=(Action("add_reminder", {"title": "Pack gym bag", "when_ts": at(21, 0), "notes": "gym tomorrow"},
                        auto=True, label="Remind me tonight"),
                 Action("add_hold", {"title": "Gym", "when_ts": at(17, 30, 1), "end_ts": at(18, 30, 1)},
                        auto=True, label="Hold an evening slot"),
                 Action("open_prompt", {"prompt": "Draft a note asking to move my 7:00 call to 9:00"},
                        auto=False, label="Draft reschedule note")))


@pytest.fixture
def store(tmp_path):
    from service.assistant.store import AssistantStore
    return AssistantStore(tmp_path / "assistant.db")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from service import config
    for key in ("WISP_BEHAVIOR", "WISP_QA_SEED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "cfg" / "config.yaml")   # no saved setting
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    br.set_runtime(None)
    yield
    br.set_runtime(None)
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


def make_runtime(tmp_path, engine=None, *, calendar=None, messages=None, clock=lambda: NOW, contact_name_fn=None):
    engine = engine or FakeEngine()
    calls = {"messages": 0}

    def messages_fn():
        calls["messages"] += 1
        return messages or {"state": "unavailable", "records": []}

    rt = br.BehaviorRuntime(
        tmp_path / "home",
        local_factory=lambda path: engine,
        messages_fn=messages_fn,
        contact_name_fn=contact_name_fn,
        calendar_sync_fn=lambda: calendar if calendar is not None else {"available": False},
        session_db_fn=lambda: (_ for _ in ()).throw(RuntimeError("no sessions in tests")),
        clock=clock)
    rt.calls = calls
    original_tick = rt.tick
    async def bound_tick(store,hub,**kwargs):
        if getattr(hub,"_fixture_recording",False):
            hub._store = store
        return await original_tick(store,hub,**kwargs)
    rt.tick = bound_tick
    return rt, engine


def tick(rt, store, hub, now=NOW, force=False):
    return asyncio.run(rt.tick(store, hub, now=now, force=force))


@pytest.fixture
def queued_delivery(tmp_path, monkeypatch, store):
    """Real Engine/Store/Hub; only candidate discovery and native source are synthetic."""
    from service.assistant.hub import Hub
    from service.behavior.engine import BehaviorEngine
    from service.behavior import nudges
    from service.publication_authority import Authority, Witness
    from service.safety import policy

    policy.set_read_only(False)
    engine = BehaviorEngine(tmp_path / "queued-engine.db", clock=lambda: NOW, tz="UTC")
    observation = Observation(id="cal:g:1", kind="event_scheduled", ts=NOW+3600,
                              subject="Arbitrary activity", source="calendar")
    engine.observe([observation])
    pattern = Pattern(id="p_gym", kind="habit", subject="arbitrary",
                      statement="Repeated activity", support=8, opportunities=10,
                      confidence=.8, evidence=(observation.id,))
    engine._store.replace_patterns([pattern],NOW)
    engine._store.meta_set("last_mined_at",NOW)
    source = Authority("synthetic-native-calendar")
    source.publish()
    runtime = br.BehaviorRuntime(tmp_path / "queued-home",local_factory=lambda path:engine,
        clock=lambda:NOW,nudge_sources_fn=lambda store,now:{"calendar":Witness.combine(source.token())})
    inputs = runtime._prepare_nudge_inputs(store,NOW)
    assert inputs is not None
    candidates = [gym_conflict_nudge()]
    monkeypatch.setattr(nudges,"build_candidates",lambda inputs:list(candidates))
    created = engine.generate_nudges(NOW)
    assert len(created)==1
    runtime._stage_queued_nudge(created[0],inputs,NOW)
    assert created[0].id in runtime._queued_delivery
    hub = Hub(store)
    yield runtime,engine,hub,created[0],source,candidates
    runtime.close()


def _delivery_counts(runtime,store):
    return tuple(store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("commitments","calendar_event_ends","assistant_events"))+tuple(
        runtime._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("actions","nudges"))


def test_queued_real_delivery_records_actions_before_event_and_supports_undo(queued_delivery,store):
    runtime,engine,hub,nudge,source,_ = queued_delivery
    async def run():
        queue = hub.subscribe()
        event = await runtime.deliver(store,hub,nudge,NOW)
        assert event is not None and event["can_undo"]
        assert len(event["auto_performed"])==2
        assert queue.get_nowait()==event
        assert queue.get_nowait()=={"type":"changed"}
        assert queue.empty()
        assert _delivery_counts(runtime,store)==(2,1,1,2,1)
        row = store.event(event["event_id"])
        assert row["payload"]=={k:v for k,v in event.items() if k!="event_id"}
        assert len(runtime.undo(store,nudge.id))==2
        assert store.event(event["event_id"])["state"]=="ack"
        hub.unsubscribe(queue)
    asyncio.run(run())


@pytest.mark.parametrize("withdraw",["reject","mute","undo","source","stop","safety"])
def test_queued_prepared_revocation_has_zero_effects(queued_delivery,store,withdraw):
    from service.behavior.engine import BehaviorEngine
    from service.publication_authority import PublicationUnavailable
    from service.safety import policy
    runtime,engine,hub,nudge,source,_ = queued_delivery
    other = BehaviorEngine(engine._store.path,clock=lambda:NOW,tz="UTC")
    async def run():
        queue = hub.subscribe()
        loop = hub.prepare_nudge_delivery(store)
        prepared = runtime._prepare_delivery(store,nudge,NOW)
        assert prepared is not None
        if withdraw=="reject":
            other.set_pattern_status("p_gym","rejected")
        elif withdraw=="mute":
            other.feedback(Feedback(nudge_id=nudge.id,verdict="wrong",ts=NOW))
        elif withdraw=="undo":
            runtime.undo(store,nudge.id)
        elif withdraw=="source":
            source.withdraw()
        elif withdraw=="stop":
            runtime.stop()
        else:
            policy.set_read_only(True)
        with pytest.raises(PublicationUnavailable):
            runtime._commit_delivery(store,hub,prepared,loop)
        assert _delivery_counts(runtime,store)==(0,0,0,0,0)
        assert queue.empty()
        if withdraw=="source":
            source.publish()
            assert runtime._prepare_delivery(store,nudge,NOW) is None
        hub.unsubscribe(queue)
    try:
        asyncio.run(run())
    finally:
        other.close()
        policy.set_read_only(False)


def test_queued_partial_delivery_raises_and_retries_without_duplicate_actions(queued_delivery,store,monkeypatch):
    from service.publication_authority import PublicationUnavailable
    runtime,engine,hub,nudge,source,_ = queued_delivery
    original = store.commit_nudge_event
    def fail(*args):
        raise PublicationUnavailable("synthetic failure after action ledger")
    async def run():
        queue = hub.subscribe()
        monkeypatch.setattr(store,"commit_nudge_event",fail)
        with pytest.raises(br.DeliveryIncomplete):
            await runtime.deliver(store,hub,nudge,NOW)
        assert _delivery_counts(runtime,store)==(2,1,0,2,0)
        assert queue.empty()
        monkeypatch.setattr(store,"commit_nudge_event",original)
        event = await runtime.deliver(store,hub,nudge,NOW)
        assert event is not None and event["can_undo"]
        assert _delivery_counts(runtime,store)==(2,1,1,2,1)
        hub.unsubscribe(queue)
    asyncio.run(run())


def test_queued_delivery_wins_before_cross_engine_rejection_ack(queued_delivery,store,monkeypatch):
    import threading
    from service.behavior.engine import BehaviorEngine
    runtime,engine,hub,nudge,source,_ = queued_delivery
    other = BehaviorEngine(engine._store.path,clock=lambda:NOW,tz="UTC")
    attempted,acknowledged = threading.Event(),threading.Event()
    errors = []
    def reject():
        attempted.set()
        try:
            other.set_pattern_status("p_gym","rejected")
        except BaseException as exc:
            errors.append(exc)
        finally:
            acknowledged.set()
    worker = threading.Thread(target=reject)
    original = store.commit_nudge_actions
    def held(lease,plan,owner):
        worker.start()
        assert attempted.wait(2)
        assert not acknowledged.wait(.03)
        return original(lease,plan,owner)
    monkeypatch.setattr(store,"commit_nudge_actions",held)
    async def run():
        event = await runtime.deliver(store,hub,nudge,NOW)
        assert event is not None
        assert _delivery_counts(runtime,store)==(2,1,1,2,1)
    try:
        asyncio.run(run())
        worker.join(2)
        assert acknowledged.is_set() and not worker.is_alive() and not errors
        assert store.event_by_key("behavior:"+nudge.id)["state"]=="pending"
        assert len(runtime.undo(store,nudge.id))==2
    finally:
        worker.join(2)
        other.close()


def test_queued_commit_refuses_reentrant_mutations_before_clock_or_sql(queued_delivery,store,monkeypatch):
    from service.publication_authority import PublicationBusy
    from service.safety import policy,grants
    runtime,engine,hub,nudge,source,_ = queued_delivery
    original = store.commit_nudge_actions
    calls = []
    def forbidden(*args,**kwargs):
        raise AssertionError("Preparation or injectable clock ran inside commit")
    def held(lease,plan,owner):
        mutations = [lambda:runtime.feedback(store,nudge.id,"wrong"),
                     lambda:runtime.undo(store,nudge.id),runtime.stop,
                     lambda:engine.set_pattern_status("p_gym","rejected"),
                     source.withdraw,lambda:policy.set_read_only(True),
                     lambda:grants.revoke("web_fetch")]
        for mutate in mutations:
            before = _delivery_counts(runtime,store)
            with pytest.raises(PublicationBusy):
                mutate()
            assert _delivery_counts(runtime,store)==before
            calls.append(1)
        with pytest.raises(PublicationBusy):
            with store.transaction():
                raise AssertionError("Ordinary transaction entered")
        return original(lease,plan,owner)
    async def run():
        loop = hub.prepare_nudge_delivery(store)
        prepared = runtime._prepare_delivery(store,nudge,NOW)
        assert prepared is not None
        from service import config
        with monkeypatch.context() as patch:
            patch.setattr(runtime,"clock",forbidden)
            patch.setattr(runtime,"policy_block",forbidden)
            patch.setattr(runtime,"_nudge_sources_fn",forbidden)
            patch.setattr(hub,"publish",forbidden)
            patch.setattr(config,"models_config",forbidden)
            patch.setattr(store,"commit_nudge_actions",held)
            assert runtime._commit_delivery(store,hub,prepared,loop)["can_undo"]
        assert len(calls)==7
    asyncio.run(run())


def test_queued_owned_write_rejects_wrong_identity_thread_closed_and_serialization(queued_delivery,store):
    import pickle
    import threading
    from dataclasses import replace
    from service.publication_authority import PublicationBusy,PublicationUnavailable,publication
    from service.assistant.store import AssistantStore
    runtime,engine,hub,nudge,source,_ = queued_delivery
    other = AssistantStore(store.path.parent / "other.db")
    prepared = runtime._prepare_delivery(store,nudge,NOW)
    assert prepared is not None
    errors = []
    with publication(prepared.witness,writes=(prepared.plan.binding,)) as lease:
        for candidate,owner,target in [(prepared.plan,object(),store),
                (prepared.plan,runtime,other),
                (replace(prepared.plan,data=prepared.plan.data+" "),runtime,store)]:
            with pytest.raises(PublicationUnavailable):
                target.commit_nudge_actions(lease,candidate,owner)
        def wrong_thread():
            try:
                store.commit_nudge_actions(lease,prepared.plan,runtime)
            except PublicationUnavailable:
                errors.append("refused")
        worker = threading.Thread(target=wrong_thread)
        worker.start()
        worker.join(2)
        assert not worker.is_alive() and errors==["refused"]
        for value in (prepared,prepared.plan,prepared.plan.binding,lease):
            with pytest.raises(TypeError):
                pickle.dumps(value)
        with pytest.raises(PublicationBusy):
            with publication(prepared.witness):
                pass
    with pytest.raises(PublicationUnavailable):
        store.commit_nudge_actions(lease,prepared.plan,runtime)
    assert _delivery_counts(runtime,store)==(0,0,0,0,0)


def test_queued_later_insertions_do_not_revoke_original_batch_grants(queued_delivery,store):
    from dataclasses import replace
    runtime,engine,hub,first,source,candidates = queued_delivery
    original = engine.queued_nudge_authority(first,NOW)
    inputs = runtime._prepare_nudge_inputs(store,NOW)
    candidates[:] = [replace(gym_conflict_nudge("n2"),kind="cancel_likely",actions=()),
                     replace(gym_conflict_nudge("n3"),kind="day_preview",actions=())]
    created = engine.generate_nudges(NOW)
    assert len(created)==2
    for nudge in created:
        runtime._stage_queued_nudge(nudge,inputs,NOW)
    assert original[1].current()
    assert engine.queued_nudge_authority(first,NOW)[1] is original[1]
    async def run():
        for nudge in [first,*created]:
            assert await runtime.deliver(store,hub,nudge,NOW) is not None
        assert _delivery_counts(runtime,store)==(2,1,3,2,3)
    asyncio.run(run())


def test_queued_expired_original_and_restart_never_rebind(queued_delivery,store):
    from service.publication_authority import PublicationUnavailable
    runtime,engine,hub,nudge,source,_ = queued_delivery
    prepared = runtime._prepare_delivery(store,nudge,NOW)
    assert prepared is not None
    reading = engine._delivery_clock.read()
    engine._delivery_clock.observe(nudge.expires_at+1,reading[1]+nudge.expires_at-NOW+1)
    async def run():
        loop = hub.prepare_nudge_delivery(store)
        with pytest.raises(PublicationUnavailable):
            runtime._commit_delivery(store,hub,prepared,loop)
        assert await runtime.deliver(store,hub,nudge,NOW) is None
        fresh = br.BehaviorRuntime(runtime.home,local_factory=lambda path:engine,clock=lambda:NOW)
        try:
            assert await fresh.deliver(store,hub,nudge,NOW) is None
        finally:
            fresh.close()
        assert _delivery_counts(runtime,store)==(0,0,0,0,0)
    asyncio.run(run())


@pytest.mark.parametrize("phase",["before_actions","after_actions","ledger","after_event","fanout"])
def test_queued_partial_failure_phases_retry_actual_effects_once(queued_delivery,store,monkeypatch,phase):
    import sqlite3
    runtime,engine,hub,nudge,source,_ = queued_delivery
    actions,event,fanout = store.commit_nudge_actions,store.commit_nudge_event,hub.publish_committed_sync
    def failure(*args,**kwargs):
        raise RuntimeError("synthetic delivery failure")
    def after_actions(*args):
        actions(*args)
        failure()
    def after_event(*args):
        event(*args)
        failure()
    async def run():
        queue = hub.subscribe()
        if phase in {"before_actions","after_actions"}:
            monkeypatch.setattr(store,"commit_nudge_actions",failure if phase=="before_actions" else after_actions)
        elif phase=="ledger":
            runtime._db.set_authorizer(lambda op,table,*args:
                sqlite3.SQLITE_DENY if op==sqlite3.SQLITE_INSERT and table=="actions" else sqlite3.SQLITE_OK)
        elif phase=="after_event":
            monkeypatch.setattr(store,"commit_nudge_event",after_event)
        else:
            monkeypatch.setattr(hub,"publish_committed_sync",failure)
        try:
            with pytest.raises(br.DeliveryIncomplete):
                await runtime.deliver(store,hub,nudge,NOW)
        finally:
            runtime._db.set_authorizer(None)
        counts = _delivery_counts(runtime,store)
        assert counts=={"before_actions":(0,0,0,0,0),"after_actions":(2,1,0,0,0),
            "ledger":(2,1,0,0,0),"after_event":(2,1,1,2,0),"fanout":(2,1,1,2,1)}[phase]
        assert queue.empty() and nudge.id in runtime._incomplete_deliveries
        monkeypatch.setattr(store,"commit_nudge_actions",actions)
        monkeypatch.setattr(store,"commit_nudge_event",event)
        monkeypatch.setattr(hub,"publish_committed_sync",fanout)
        result = await runtime.deliver(store,hub,nudge,NOW)
        assert result is not None and result["can_undo"]
        assert _delivery_counts(runtime,store)==(2,1,1,2,1)
        assert nudge.id not in runtime._incomplete_deliveries
        assert queue.get_nowait()==result and queue.get_nowait()=={"type":"changed"}
        assert queue.empty()
        hub.unsubscribe(queue)
    asyncio.run(run())


def test_queued_tick_keeps_partial_cursor_and_never_silently_drops_withdrawn_retry(queued_delivery,store,monkeypatch):
    runtime,engine,hub,nudge,source,_ = queued_delivery
    monkeypatch.setattr(runtime,"work",lambda *args,**kwargs:([nudge],"local"))
    original = store.commit_nudge_event
    def failure(*args):
        raise RuntimeError("event write failed")
    async def run():
        monkeypatch.setattr(store,"commit_nudge_event",failure)
        assert await runtime.tick(store,hub,now=NOW)==[]
        assert runtime.kv_get("cursor:local","0")=="0"
        assert "DeliveryIncomplete" in runtime.errors["deliver"]
        assert _delivery_counts(runtime,store)==(2,1,0,2,0)
        source.withdraw()
        monkeypatch.setattr(store,"commit_nudge_event",original)
        assert await runtime.tick(store,hub,now=NOW)==[]
        assert runtime.kv_get("cursor:local","0")=="0"
        assert "DeliveryIncomplete" in runtime.errors["deliver"]
        assert _delivery_counts(runtime,store)==(2,1,0,2,0)
    asyncio.run(run())


def test_queued_logical_commit_finishes_when_time_expires_during_own_sql(queued_delivery,store,monkeypatch):
    runtime,engine,hub,nudge,source,_ = queued_delivery
    original = store.commit_nudge_actions
    def expired(lease,plan,owner):
        reading = engine._delivery_clock.read()
        engine._delivery_clock.observe(nudge.expires_at+1,reading[1]+nudge.expires_at-NOW+1)
        assert not lease.current()
        return original(lease,plan,owner)
    monkeypatch.setattr(store,"commit_nudge_actions",expired)
    async def run():
        result = await runtime.deliver(store,hub,nudge,NOW)
        assert result is not None and result["can_undo"]
        assert _delivery_counts(runtime,store)==(2,1,1,2,1)
    asyncio.run(run())


def test_queued_copy_cannot_change_prepared_identity_or_committed_payload(queued_delivery,store):
    from dataclasses import replace
    from service.publication_authority import PublicationUnavailable,publication
    runtime,engine,hub,nudge,source,_ = queued_delivery
    prepared = runtime._prepare_delivery(store,nudge,NOW)
    async def run():
        loop = hub.prepare_nudge_delivery(store)
        for copy in (replace(prepared),replace(prepared,nudge_id="forged")):
            with pytest.raises(PublicationUnavailable):
                runtime._commit_delivery(store,hub,copy,loop)
        with publication(prepared.witness,writes=(prepared.plan.binding,)) as lease:
            with pytest.raises(PublicationUnavailable):
                store.commit_nudge_actions(lease,replace(prepared.plan),runtime)
        original = store.commit_nudge_event
        def validate(lease,plan,owner):
            receipt = original(lease,plan,owner)
            with pytest.raises(PublicationUnavailable):
                hub.publish_committed_sync(lease,replace(receipt,payload='{"type":"forged"}'),runtime,loop)
            return receipt
        store.commit_nudge_event = validate
        try:
            assert runtime._commit_delivery(store,hub,prepared,loop)["nudge_id"]==nudge.id
            assert _delivery_counts(runtime,store)==(2,1,1,2,1)
        finally:
            store.commit_nudge_event = original
    asyncio.run(run())


def test_queued_preparation_and_commit_refuse_caller_transactions(queued_delivery,store):
    from service.publication_authority import PublicationUnavailable
    runtime,engine,hub,nudge,source,_ = queued_delivery
    runtime._db.execute("BEGIN")
    try:
        with pytest.raises(PublicationUnavailable):
            runtime._prepare_delivery(store,nudge,NOW)
        assert runtime._db.in_transaction
    finally:
        runtime._db.rollback()
    store._db.execute("BEGIN")
    try:
        with pytest.raises(PublicationUnavailable):
            runtime._prepare_delivery(store,nudge,NOW)
        assert store._db.in_transaction
    finally:
        store._db.rollback()
    prepared = runtime._prepare_delivery(store,nudge,NOW)
    async def run():
        loop = hub.prepare_nudge_delivery(store)
        runtime._db.execute("BEGIN")
        try:
            with pytest.raises(PublicationUnavailable):
                runtime._commit_delivery(store,hub,prepared,loop)
            assert runtime._db.in_transaction
        finally:
            runtime._db.rollback()
        store._db.execute("BEGIN")
        try:
            with pytest.raises(br.DeliveryIncomplete):
                runtime._commit_delivery(store,hub,prepared,loop)
            assert store._db.in_transaction
        finally:
            store._db.rollback()
        assert _delivery_counts(runtime,store)==(0,0,0,0,0)
    asyncio.run(run())


def test_queued_missing_evidence_is_not_an_implicit_source_grant(queued_delivery,store):
    from dataclasses import replace
    runtime,engine,hub,nudge,source,candidates = queued_delivery
    inputs = runtime._prepare_nudge_inputs(store,NOW)
    candidates[:] = [replace(gym_conflict_nudge("unknown"),kind="day_preview",evidence=("missing:evidence",))]
    [unknown] = engine.generate_nudges(NOW)
    runtime._stage_queued_nudge(unknown,inputs,NOW)
    assert unknown.id not in runtime._queued_delivery
    assert asyncio.run(runtime.deliver(store,hub,unknown,NOW)) is None
    assert _delivery_counts(runtime,store)==(0,0,0,0,0)


def test_queued_safety_aba_during_full_preparation_never_rearms_original(queued_delivery,store,monkeypatch):
    from service.safety import policy
    runtime,engine,hub,nudge,source,_ = queued_delivery
    original = runtime.policy_block
    checked = []
    def changed(action):
        result = original(action)
        policy.set_read_only(True)
        policy.set_read_only(False)
        checked.append(1)
        return result
    monkeypatch.setattr(runtime,"policy_block",changed)
    assert runtime._prepare_delivery(store,nudge,NOW) is None
    assert checked and not policy.read_only()
    assert _delivery_counts(runtime,store)==(0,0,0,0,0)


def test_queued_actual_hub_replays_canonical_event_and_preserves_legacy_refusal(queued_delivery,store):
    runtime,engine,hub,nudge,source,_ = queued_delivery
    async def run():
        result = await runtime.deliver(store,hub,nudge,NOW)
        stream = hub.events(rotate_after=.1)
        try:
            replay = await anext(stream)
            assert replay==result
        finally:
            await stream.aclose()
        canonical = await hub.publish({"type":"nudge","body":"forged"},dedupe_key="behavior:"+nudge.id)
        assert canonical==result
        for kind in ("create_apple_reminder","update_apple_reminder","delete_apple_reminder"):
            with pytest.raises(ValueError,match="legacy Reminders"):
                await hub.publish({"type":kind})
        assert _delivery_counts(runtime,store)==(2,1,1,2,1)
        future = asyncio.new_event_loop()
        hub._owner_loop = future
        try:
            with pytest.raises(ValueError,match="another event loop"):
                hub.prepare_nudge_delivery(store)
        finally:
            future.close()
    asyncio.run(run())


def test_queued_withheld_cursor_advances_without_rearming_or_spending_budget(queued_delivery,store,monkeypatch):
    runtime,engine,hub,nudge,source,_ = queued_delivery
    runtime._stamps["local"] = {"ingest":NOW,"mine":NOW,"nudge":0}
    runtime._availability_attempt = NOW
    runtime._card = (NOW,"")
    monkeypatch.setattr(runtime,"_refresh_availability",lambda now:None)
    source.withdraw()
    async def run():
        assert await runtime.tick(store,hub,now=NOW)==[]
        assert runtime.kv_get("cursor:local")==str(nudge.seq)
        source.publish()
        assert await runtime.tick(store,hub,now=NOW+61)==[]
        assert runtime.kv_get("cursor:local")==str(nudge.seq)
        assert len(engine.nudges_since(0)[0])==1
        assert _delivery_counts(runtime,store)==(0,0,0,0,0)
    asyncio.run(run())


@pytest.mark.parametrize("with_ledger",[False,True])
def test_queued_partial_recovery_never_adopts_completed_or_edited_items(queued_delivery,store,monkeypatch,with_ledger):
    runtime,engine,hub,nudge,source,_ = queued_delivery
    actions,event = store.commit_nudge_actions,store.commit_nudge_event
    def failure(*args):
        if not with_ledger:
            actions(*args)
        raise RuntimeError("synthetic partial action")
    async def run():
        target = "commit_nudge_event" if with_ledger else "commit_nudge_actions"
        monkeypatch.setattr(store,target,failure)
        with pytest.raises(br.DeliveryIncomplete):
            await runtime.deliver(store,hub,nudge,NOW)
        with store.transaction() as db:
            db.execute("UPDATE commitments SET status='completed',updated_at=? WHERE kind='reminder'",(NOW+1,))
            db.execute("UPDATE commitments SET title='User edit',updated_at=? WHERE kind='event'",(NOW+1,))
        monkeypatch.setattr(store,"commit_nudge_actions",actions)
        monkeypatch.setattr(store,"commit_nudge_event",event)
        result = await runtime.deliver(store,hub,nudge,NOW)
        assert result is not None and not result["can_undo"] and result["auto_performed"]==[]
        assert len(store._db.execute("SELECT * FROM commitments").fetchall())==2
        assert store._db.execute("SELECT title FROM commitments WHERE kind='event'").fetchone()[0]=="User edit"
        assert len(runtime.actions_for(nudge.id))==(2 if with_ledger else 0)
    asyncio.run(run())


@pytest.mark.parametrize("writer",["add_manual","set_status","mark_notified"])
def test_queued_legacy_store_writers_refuse_reentry_without_owned_capability(queued_delivery,store,writer,monkeypatch):
    import sqlite3
    from service.assistant import store as store_module
    from service.publication_authority import PublicationBusy,publication
    runtime,engine,hub,nudge,source,_ = queued_delivery
    manual = store.add_manual("Existing user item",NOW+600)
    prepared = runtime._prepare_delivery(store,nudge,NOW)
    operations = {"add_manual":lambda:store.add_manual("Unowned write",NOW+300),
                  "set_status":lambda:store.set_status(manual["id"],"completed"),
                  "mark_notified":lambda:store.mark_notified(manual["id"],"due")}
    with publication(prepared.witness,writes=(prepared.plan.binding,)):
        def forbidden(*args,**kwargs):
            raise AssertionError("Store mutation reached clock/UUID/query before Busy")
        with monkeypatch.context() as patch:
            patch.setattr(store_module.time,"time",forbidden)
            patch.setattr(store_module.uuid,"uuid4",forbidden)
            patch.setattr(store,"duplicate_ids",forbidden)
            store._db.set_authorizer(lambda *args:sqlite3.SQLITE_DENY)
            try:
                with pytest.raises(PublicationBusy):
                    operations[writer]()
            finally:
                store._db.set_authorizer(None)
    assert store.get(manual["id"])["status"]=="active"
    assert len(store._db.execute("SELECT * FROM commitments").fetchall())==1
    assert store._db.execute("SELECT COUNT(*) FROM notify_log").fetchone()[0]==0


@pytest.mark.parametrize("writer",["add_manual","set_status","mark_notified"])
def test_queued_legacy_store_writer_waits_for_actual_delivery(queued_delivery,store,writer,monkeypatch):
    import threading
    runtime,engine,hub,nudge,source,_ = queued_delivery
    manual = store.add_manual("Existing user item",NOW+600)
    attempted,acknowledged = threading.Event(),threading.Event()
    errors = []
    operations = {"add_manual":lambda:store.add_manual("User addition",NOW+300),
                  "set_status":lambda:store.set_status(manual["id"],"completed"),
                  "mark_notified":lambda:store.mark_notified(manual["id"],"due")}
    def mutate():
        attempted.set()
        try:
            operations[writer]()
        except BaseException as exc:
            errors.append(exc)
        finally:
            acknowledged.set()
    worker = threading.Thread(target=mutate)
    original = store.commit_nudge_actions
    def held(*args):
        worker.start()
        assert attempted.wait(2) and not acknowledged.wait(.03)
        return original(*args)
    monkeypatch.setattr(store,"commit_nudge_actions",held)
    try:
        assert asyncio.run(runtime.deliver(store,hub,nudge,NOW))["can_undo"]
    finally:
        worker.join(2)
    assert acknowledged.is_set() and not worker.is_alive() and errors==[]
    if writer=="add_manual":
        assert store._db.execute("SELECT COUNT(*) FROM commitments WHERE title='User addition'").fetchone()[0]==1
    elif writer=="set_status":
        assert store.get(manual["id"])["status"]=="completed"
    else:
        assert store.already_notified(manual["id"],"due")
    # Ordinary nested writers remain part of their caller's rollback unit.
    with pytest.raises(RuntimeError,match="rollback"):
        with store.transaction():
            store.add_manual("Rolled back",NOW+900)
            store.set_status(manual["id"],"active")
            store.mark_notified(manual["id"],"rolled-back")
            raise RuntimeError("rollback")
    assert store._db.execute("SELECT COUNT(*) FROM commitments WHERE title='Rolled back'").fetchone()[0]==0
    assert not store.already_notified(manual["id"],"rolled-back")


@pytest.mark.parametrize("table",["commitments","calendar_event_ends","assistant_behavior_action_receipts"])
def test_queued_store_item_end_and_receipt_creation_are_atomic(queued_delivery,store,table):
    runtime,engine,hub,nudge,source,_ = queued_delivery
    store._db.execute(f"CREATE TEMP TRIGGER fail_atomic BEFORE INSERT ON {table} "
                      "BEGIN SELECT RAISE(ABORT,'synthetic atomic failure'); END")
    async def run():
        queue = hub.subscribe()
        with pytest.raises(br.DeliveryIncomplete):
            await runtime.deliver(store,hub,nudge,NOW)
        assert _delivery_counts(runtime,store)==(0,0,0,0,0) and queue.empty()
        assert store._db.execute("SELECT COUNT(*) FROM assistant_behavior_action_receipts").fetchone()[0]==0
        store._db.execute("DROP TRIGGER fail_atomic")
        result = await runtime.deliver(store,hub,nudge,NOW)
        assert result is not None and result["can_undo"]
        assert _delivery_counts(runtime,store)==(2,1,1,2,1)
        assert store._db.execute("SELECT COUNT(*) FROM assistant_behavior_action_receipts WHERE receipt_state='active'").fetchone()[0]==2
        hub.unsubscribe(queue)
    asyncio.run(run())


@pytest.mark.parametrize("change",["receipt","item","end","cid"])
def test_queued_receipt_item_and_end_cas_across_two_store_connections(queued_delivery,store,monkeypatch,change):
    from service.assistant.store import AssistantStore
    runtime,engine,hub,nudge,source,_ = queued_delivery
    original = store.commit_nudge_event
    def fail(*args):
        raise RuntimeError("synthetic event failure")
    async def run():
        monkeypatch.setattr(store,"commit_nudge_event",fail)
        with pytest.raises(br.DeliveryIncomplete):
            await runtime.deliver(store,hub,nudge,NOW)
        monkeypatch.setattr(store,"commit_nudge_event",original)
        prepared = runtime._prepare_delivery(store,nudge,NOW)
        assert prepared is not None
        other = AssistantStore(store.path)
        try:
            with other.transaction() as db:
                if change=="receipt":
                    db.execute("UPDATE assistant_behavior_action_receipts SET created_at=created_at+1 WHERE action_index=0")
                elif change=="item":
                    db.execute("UPDATE commitments SET title='User edit',updated_at=updated_at+1 WHERE kind='reminder'")
                elif change=="end":
                    db.execute("UPDATE calendar_event_ends SET end_ts=end_ts+60")
                else:
                    db.execute("UPDATE assistant_behavior_action_receipts SET commitment_id='changed-cid' WHERE action_index=0")
            before = _delivery_counts(runtime,store)
            loop = hub.prepare_nudge_delivery(store)
            queue = hub.subscribe()
            try:
                with pytest.raises(br.DeliveryIncomplete):
                    runtime._commit_delivery(store,hub,prepared,loop)
                assert _delivery_counts(runtime,store)==before and queue.empty()
            finally:
                hub.unsubscribe(queue)
        finally:
            other._db.close()
    asyncio.run(run())


def test_behavior_receipt_upgrade_reopen_preserves_legacy_store(tmp_path):
    from service.assistant.store import AssistantStore,behavior_owner_id
    path = tmp_path/"legacy.db"
    original = AssistantStore(path)
    manual = original.add_manual("Existing legacy item",NOW+600)
    event = original.enqueue_event({"type":"nudge","body":"Existing event"},dedupe_key="existing")
    with original.transaction() as db:
        db.execute("DROP TABLE assistant_behavior_action_receipts")
    original._db.close()
    for _ in range(2):
        upgraded = AssistantStore(path)
        try:
            assert upgraded.get(manual["id"])["title"]=="Existing legacy item"
            assert upgraded.event(event["id"])["payload"]["body"]=="Existing event"
            assert upgraded.forget_behavior_actions(behavior_owner_id(tmp_path/"unused-home"))=={
                "removed":[],"kept":0,"covered":()}
        finally:
            upgraded._db.close()


@pytest.mark.parametrize("malformed",["missing_columns","missing_unique","wrong_state","wrong_pk"])
def test_behavior_receipt_malformed_preexisting_schema_refuses_without_replacement(tmp_path,malformed):
    from service.assistant.store import AssistantStore,_BEHAVIOR_RECEIPTS_SCHEMA
    path = tmp_path/"malformed.db"
    original = AssistantStore(path)
    manual = original.add_manual("Preserve this item",NOW+600)
    definitions = {"missing_columns":"CREATE TABLE assistant_behavior_action_receipts(owner_id TEXT)",
        "missing_unique":_BEHAVIOR_RECEIPTS_SCHEMA.replace("commitment_id TEXT NOT NULL UNIQUE","commitment_id TEXT NOT NULL"),
        "wrong_state":_BEHAVIOR_RECEIPTS_SCHEMA.replace("'active','consumed'","'acti ve','consumed'"),
        "wrong_pk":_BEHAVIOR_RECEIPTS_SCHEMA.replace("PRIMARY KEY(owner_id,nudge_id,action_index)","PRIMARY KEY(owner_id,action_index,nudge_id)")}
    with original.transaction() as db:
        db.execute("DROP TABLE assistant_behavior_action_receipts")
        db.execute(definitions[malformed])
    before = original._db.execute("SELECT sql FROM sqlite_master WHERE name='assistant_behavior_action_receipts'").fetchone()[0]
    original._db.close()
    with pytest.raises(RuntimeError,match="receipt schema"):
        AssistantStore(path)
    import sqlite3
    with sqlite3.connect(path) as inspect:
        assert inspect.execute("SELECT title FROM commitments WHERE id=?",(manual["id"],)).fetchone()[0]=="Preserve this item"
        assert inspect.execute("SELECT sql FROM sqlite_master WHERE name='assistant_behavior_action_receipts'").fetchone()[0]==before


def test_behavior_owner_identity_uses_one_canonical_home_encoding(tmp_path):
    from service.assistant.store import behavior_owner_id
    home = tmp_path/"home"
    home.mkdir()
    alias = tmp_path/"alias"
    alias.symlink_to(home,target_is_directory=True)
    assert behavior_owner_id(home)==behavior_owner_id(alias)==behavior_owner_id(home/".."/"home")
    assert behavior_owner_id(tmp_path/"different")!=behavior_owner_id(home)


@pytest.mark.parametrize("collision",["cid","primary_key"])
def test_queued_receipt_identity_collision_is_refused_before_effects(queued_delivery,store,collision):
    from service.publication_authority import PublicationUnavailable
    runtime,engine,hub,nudge,source,_ = queued_delivery
    prepared = runtime._prepare_delivery(store,nudge,NOW)
    data = json.loads(prepared.plan.data)
    action = data["units"][0]
    owner = "f"*64 if collision=="cid" else runtime._owner_id
    cid = action["id"] if collision=="cid" else "foreign-commitment-id"
    with store.transaction() as db:
        db.execute("INSERT INTO assistant_behavior_action_receipts "
            "(owner_id,nudge_id,action_index,commitment_id,receipt_state,type,kind,title,context,when_ts,end_ts,created_at) "
            "VALUES(?,?,?,?,'active',?,?,?,?,?,?,?)",(owner,nudge.id,action["idx"],cid,action["type"],
            action["kind"],action["title"],action["context"],action["when_ts"],action["end_ts"],NOW))
    with pytest.raises(PublicationUnavailable,match="ownership identity conflicts"):
        runtime._prepare_delivery(store,nudge,NOW)
    assert _delivery_counts(runtime,store)==(0,0,0,0,0)


def test_queued_consumed_receipts_never_recreate_items_or_adopt_reused_cid(queued_delivery,store,monkeypatch):
    runtime,engine,hub,nudge,source,_ = queued_delivery
    original = store.commit_nudge_event
    def fail(*args):
        raise RuntimeError("synthetic event failure")
    async def run():
        monkeypatch.setattr(store,"commit_nudge_event",fail)
        with pytest.raises(br.DeliveryIncomplete):
            await runtime.deliver(store,hub,nudge,NOW)
        monkeypatch.setattr(store,"commit_nudge_event",original)
        old = dict(store._db.execute("SELECT * FROM commitments WHERE kind='reminder'").fetchone())
        cleaned = store.forget_behavior_actions(runtime._owner_id)
        assert len(cleaned["removed"])==2
        assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==0
        # Reuse the exact CID with matching old text/time/creation identity:
        # consumed ownership is still a permanent no-recreation disposition.
        with store.transaction() as db:
            columns = list(old)
            db.execute("INSERT INTO commitments("+",".join(columns)+") VALUES("+",".join("?" for _ in columns)+")",
                       tuple(old.values()))
        result = await runtime.deliver(store,hub,nudge,NOW)
        assert result is not None and not result["can_undo"] and result["auto_performed"]==[]
        again = store.forget_behavior_actions(runtime._owner_id)
        assert again["removed"]==[] and again["kept"]==1 and len(again["covered"])==2
        assert store.get(old["id"]) is not None
        assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==1
        assert store._db.execute("SELECT COUNT(*) FROM assistant_behavior_action_receipts WHERE receipt_state='consumed'").fetchone()[0]==2
    asyncio.run(run())


# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------

def test_on_by_default(tmp_path):
    from service import behavior_switch as flags
    assert br.enabled() and flags.env_override() is None and flags.setting_enabled()
    rt, _ = make_runtime(tmp_path)
    br.set_runtime(rt)
    assert br.get_runtime() is rt


def test_default_runtime_reads_the_real_sources_and_keeps_message_text_out(tmp_path, monkeypatch, store):
    """On by default means real data: the default collaborators are the real Messages reader,
    the real Calendar-synced store and the real session history, never a demo feed, and the
    Messages path keeps who/when only."""
    from service.tools import imessage_tools
    engine = FakeEngine()
    rt = br.BehaviorRuntime(home=tmp_path / "home", local_factory=lambda path: engine)
    assert rt._messages_fn is br._default_messages
    assert rt._calendar_sync_fn is br._default_calendar_sync
    assert rt._session_db_fn is br._default_session_db
    seen = []
    secret = "SECRET BODY TEXT"
    snapshot = {"state": "ready", "records": [
        {"identity": "m1", "timestamp": NOW - 600, "direction": "incoming", "sender": "+15555550101", "text": secret},
        {"identity": "m2", "timestamp": NOW - 300, "direction": "outgoing", "conversation": "any;-;+15555550101",
         "text": secret}]}
    monkeypatch.setattr(imessage_tools, "structured_messages_snapshot", lambda: seen.append(1) or snapshot)
    assert rt._ingest_messages(NOW) is True and seen == [1]
    assert {o.source for o in engine.obs.values()} == {"messages"}
    assert {o.kind for o in engine.obs.values()} == {"message_in", "message_out"}
    assert secret not in json.dumps([o.to_dict() if hasattr(o, "to_dict") else vars(o) for o in engine.obs.values()],
                                    default=str)
    # Calendar rows come from the store the Swift app syncs into.
    store.sync_source("calendar", [{"kind": "event", "source_id": "e1", "title": "Standup", "when_ts": NOW + 3600,
                                    "context": "Work", "all_day": False}])
    rows = rt._calendar_rows(store, NOW - 86400, NOW + 86400)
    assert [o.source for o in rt.calendar_observations(rows)] == ["calendar"]


def test_explicitly_off_does_nothing(monkeypatch, store):
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    assert not br.enabled() and br.get_runtime() is None
    assert asyncio.run(br.tick(store, FakeHub())) == []
    br.record_decision("send_message", True)          # no-op, no error
    assert br.profile_card() == ""


def test_saved_setting_off_stops_everything_and_env_one_overrides_it(monkeypatch, store, tmp_path):
    from service import config
    rt, _ = make_runtime(tmp_path)
    br.set_runtime(rt)
    config.set_behavior_enabled(False)
    assert not br.enabled() and br.get_runtime() is None
    assert asyncio.run(br.tick(store, FakeHub())) == []
    monkeypatch.setenv("WISP_BEHAVIOR", "1")           # explicit override beats the saved choice
    assert br.enabled() and br.get_runtime() is rt
    monkeypatch.delenv("WISP_BEHAVIOR")
    config.set_behavior_enabled(True)
    assert br.enabled()


def test_behavior_zero_and_other_values_are_off(monkeypatch):
    from service.behavior_switch import enabled as env_enabled
    from service.memory.prompt_blocks import profile_block
    from service.tools.behavior_tools import _unavailable
    monkeypatch.setenv("WISP_BEHAVIOR", "yes")
    assert not env_enabled()
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    assert not env_enabled() and not br.enabled()
    assert profile_block() == "" and _unavailable()
    monkeypatch.setenv("WISP_BEHAVIOR", "")            # empty is "unset": the setting/default decides
    assert env_enabled()
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    assert env_enabled() and not _unavailable()


# --------------------------------------------------------------------------
# Ingest -> mine -> nudge -> hub
# --------------------------------------------------------------------------

def test_nudge_published_with_app_event_shape_and_auto_actions(tmp_path, store):
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    hub = FakeHub()
    [row] = tick(rt, store, hub)
    nudge_events = [e for e in hub.events if e["event"]["type"] == "nudge"]
    assert len(nudge_events) == 1
    sent = nudge_events[0]
    ev = sent["event"]
    assert sent["dedupe_key"] == "behavior:n1" and sent["expires_at"] == at(7, 0, 1)
    for key in ("type", "nudge_id", "title", "body", "because"):
        assert ev[key]
    assert ev["nudge_id"] == "n1" and ev["kind"] == "habit_conflict"
    assert [a["type"] for a in ev["actions"]] == ["add_reminder", "add_hold", "open_prompt"]
    assert ev["can_undo"] is True
    assert [a["type"] for a in ev["auto_performed"]] == ["add_reminder", "add_hold"]
    assert store.event(row["event_id"])["dedupe_key"]=="behavior:n1"
    # The reversible actions landed on Wisp's own schedule; open_prompt did not run.
    rows = {r["title"]: r for r in (dict(x) for x in store._db.execute("SELECT * FROM commitments"))}
    assert set(rows) == {"Pack gym bag", "Tentative: Gym"}
    assert rows["Pack gym bag"]["kind"] == "reminder" and rows["Pack gym bag"]["when_ts"] == at(21, 0)
    assert rows["Pack gym bag"]["context"].startswith(br.BEHAVIOR_CONTEXT)
    assert rows["Tentative: Gym"]["kind"] == "event" and rows["Tentative: Gym"]["source"] == "manual"
    end = store._db.execute("SELECT end_ts FROM calendar_event_ends WHERE commitment_id=?",
                            (rows["Tentative: Gym"]["id"],)).fetchone()[0]
    assert end == at(18, 30, 1)
    # A changed ping lets the app refresh its schedule views.
    assert any(e["event"] == {"type": "changed"} and e["durable"] is False for e in hub.events)


def test_second_tick_does_not_republish_or_duplicate(tmp_path, store):
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    hub = FakeHub()
    tick(rt, store, hub)
    assert tick(rt, store, hub, now=NOW + 120, force=True) == []
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2
    assert rt.status()["cursor"]["local"] == 1


def test_rate_limits_between_ticks(tmp_path, store):
    rt, eng = make_runtime(tmp_path)
    hub = FakeHub()
    tick(rt, store, hub)
    tick(rt, store, hub, now=NOW + 30)
    assert eng.mined == 1 and eng.generated == 1              # nothing new, too soon
    tick(rt, store, hub, now=NOW + br.NUDGE_EVERY_S + 1)
    assert eng.generated == 2 and eng.mined == 1
    tick(rt, store, hub, now=NOW + br.MINE_EVERY_S + 1)
    assert eng.mined == 2


def test_expired_nudge_is_skipped_but_cursor_advances(tmp_path, store):
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge(expires=NOW - 1)]))
    hub = FakeHub()
    assert tick(rt, store, hub) == []
    assert hub.events == [] and rt.status()["cursor"]["local"] == 1
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0


def test_undo_removes_auto_actions_and_records_feedback(tmp_path, store):
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    tick(rt, store, FakeHub())
    removed = rt.undo(store, "n1")
    assert sorted(r["title"] for r in removed) == ["Pack gym bag", "Tentative: Gym"]
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0
    assert store._db.execute("SELECT COUNT(*) FROM calendar_event_ends").fetchone()[0] == 0
    assert [f.verdict for f in eng.feedbacks] == ["undo"] and eng.feedbacks[0].nudge_id == "n1"
    assert rt.undo(store, "n1") == []                        # idempotent
    # An undone action is never silently recreated by a retry.
    assert rt.perform_action(store, "n1", 0, gym_conflict_nudge().actions[0], NOW) is None


def test_auto_action_skips_existing_item_and_past_times(tmp_path, store):
    rt, _ = make_runtime(tmp_path)
    store.add_manual("Pack gym bag", at(21, 0))
    nudge = gym_conflict_nudge()
    assert rt.perform_action(store, "n1", 0, nudge.actions[0], NOW) is None      # already on file
    past = Action("add_reminder", {"title": "Too late", "when_ts": NOW - 3600}, auto=True)
    assert rt.perform_action(store, "n1", 5, past, NOW) is None
    assert rt.perform_action(store, "n1", 2, nudge.actions[2], NOW) is None      # open_prompt never


def test_real_hub_persists_nudge_and_expiry_retires_it(tmp_path, store):
    from service.assistant.hub import Hub
    hub = Hub(store)
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge(expires=NOW + 600)]))
    [row] = tick(rt, store, hub)
    ev = store.event_by_key("behavior:n1")
    assert ev["kind"] == "nudge" and ev["state"] == "pending" and ev["expires_at"] == NOW + 600
    assert row["event_id"] == ev["id"]
    # Reminder invalidation never touches nudge events.
    assert [e["id"] for e in store.pending_events() if e["kind"] == "nudge"] == [ev["id"]]
    # The app's generic acknowledgement works for kind=nudge.
    assert store.acknowledge_event(ev["id"], "nudge") is True
    # Expiry: a second nudge past its expiry is retired so the hub stops replaying it.
    rt2, _ = make_runtime(tmp_path / "b", FakeEngine(nudges=[gym_conflict_nudge("n2", expires=NOW + 60)]))
    tick(rt2, store, hub)
    assert store.event_by_key("behavior:n2")["state"] == "pending"
    rt2.expire_events(store, NOW + 61)
    assert store.event_by_key("behavior:n2")["state"] == "superseded"


def test_feedback_retires_pending_event(tmp_path, store):
    from service.assistant.hub import Hub
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge(expires=NOW + 3600)]))
    tick(rt, store, Hub(store))
    rt.feedback(store, "n1", "not_now")
    assert store.event_by_key("behavior:n1")["state"] == "ack"
    assert eng.feedbacks[-1].verdict == "not_now"


# --------------------------------------------------------------------------
# Calendar snapshot mapping (cancellations are detected by the engine diff)
# --------------------------------------------------------------------------

def _cal(title, when, *, sid, calendar="Work", end=None, all_day=False):
    return {"source_id": sid, "kind": "meeting", "title": title, "context": calendar, "when_ts": when,
            "end_ts": end, "all_day": all_day, "confidence": 1.0}


def test_calendar_snapshot_maps_ids_and_detects_cancellation(tmp_path, store):
    items = [_cal("Gym", at(6, 30, -1), sid="g", end=at(8, 0, -1)),
             _cal("Team sync", at(16, 0, 1), sid="t", end=at(16, 30, 1)),
             _cal("Board prep", at(17, 0, 1), sid="b"),
             _cal("Holiday", at(0, 0, 2), sid="h", all_day=True),
             _cal("Real dentist", at(11, 0, 1), sid="r", calendar="Home")]
    store.sync_source("calendar", items)
    sync = {"available": True, "last_sync": NOW - 5}
    rt, eng = make_runtime(tmp_path, calendar=sync)
    tick(rt, store, FakeHub())
    source, ids, start, end, taken = eng.snapshot_calls[-1]
    assert source == "calendar" and taken == NOW - 5
    # All-day plans stay available for calendar-load reports; habit miners exclude them.
    assert sorted(ids) == sorted([f"cal:g:{int(at(6, 30, -1))}", f"cal:t:{int(at(16, 0, 1))}",
                                  f"cal:b:{int(at(17, 0, 1))}", f"cal:r:{int(at(11, 0, 1))}",
                                  f"cal:h:{int(at(0, 0, 2))}"])
    sync_ = eng.obs[f"cal:t:{int(at(16, 0, 1))}"]
    assert sync_.kind == "event" and sync_.end_ts == at(16, 30, 1) and sync_.source_id == "t"
    assert sync_.attrs["calendar"] == "Work"
    assert eng.obs[f"cal:r:{int(at(11, 0, 1))}"].attrs["calendar"] == "Home"
    mined = eng.mined
    # The user deletes "Board prep"; the app re-syncs the calendar.
    store.sync_source("calendar", [i for i in items if i["source_id"] != "b"])
    sync["last_sync"] = NOW + 10
    tick(rt, store, FakeHub(), now=NOW + 20)              # a fresh sync is ingested immediately
    assert f"x:cal:b:{int(at(17, 0, 1))}" in eng.obs      # engine diff saw the cancellation
    assert eng.mined == mined + 1                         # changed -> re-mined right away


def test_wisp_marked_done_calendar_row_is_not_a_cancellation(tmp_path, store):
    items = [_cal("Team sync", at(16, 0, 1), sid="t"), _cal("Board prep", at(17, 0, 1), sid="b")]
    items[1]["organizer"] = "Nora Keene"
    store.sync_source("calendar", items)
    sync = {"available": True, "last_sync": NOW - 5}
    rt, eng = make_runtime(tmp_path, calendar=sync)
    tick(rt, store, FakeHub())
    team, board = eng.obs[f"cal:t:{int(at(16, 0, 1))}"], eng.obs[f"cal:b:{int(at(17, 0, 1))}"]
    assert team.attrs["organizer_is_user"] is True and team.attrs["organizer"] == ""
    assert board.attrs["organizer_is_user"] is False and board.attrs["organizer"] == "Nora Keene"
    # The user marks "Board prep" done inside Wisp; Apple Calendar still has it.
    cid = store._db.execute("SELECT id FROM commitments WHERE source_id='b'").fetchone()[0]
    store.set_status(cid, "done")
    sync["last_sync"] = NOW + 10
    tick(rt, store, FakeHub(), now=NOW + 20)
    assert not any(k.startswith("x:") for k in eng.obs)
    assert f"cal:b:{int(at(17, 0, 1))}" in eng.snapshot_calls[-1][1]


def test_calendar_not_read_until_a_completed_sync(tmp_path, store):
    store.sync_source("calendar", [_cal("Gym", at(6, 30, 1), sid="g")])
    rt, eng = make_runtime(tmp_path, calendar={"available": False, "syncing": True})
    tick(rt, store, FakeHub())
    assert eng.snapshot_calls == []


def test_real_mode_reads_all_calendars_messages_metadata_and_reminders(tmp_path, store):
    store.sync_source("calendar", [_cal("Standup", at(10, 0, 1), sid="s", calendar="Work")])
    store.add_manual("Call mom", at(18, 0))
    messages = {"state": "ready", "records": [
        {"identity": "m1", "timestamp": NOW - 600, "direction": "incoming", "sender": "+15555550101",
         "conversation": "any;-;+15555550101", "text": "secret text never ingested"},
        {"identity": "m2", "timestamp": NOW - 300, "direction": "outgoing", "sender": "",
         "conversation": "iMessage;-;priya@example.invalid", "text": "also secret"}]}
    names = {"+15555550101": "Sam", "priya@example.invalid": "Priya"}
    rt, eng = make_runtime(tmp_path, calendar={"available": True, "last_sync": NOW},
                           messages=messages, contact_name_fn=lambda h: names.get(h, h))
    store.sync_source("calendar", [_cal("Standup", at(10, 0, 1), sid="s", calendar="Work"),
                                   _cal("Old 1:1", at(15, 0, -7), sid="o", calendar="Work",
                                        end=at(15, 30, -7))])
    tick(rt, store, FakeHub())
    assert any(k.startswith("cal:s:") for k in eng.obs)
    assert eng.obs[f"cal:o:{int(at(15, 0, -7))}"].end_ts == at(15, 30, -7)     # real mode keeps history
    assert eng.obs["msg:m1"].kind == "message_in" and eng.obs["msg:m1"].subject == "Sam"
    assert eng.obs["msg:m2"].kind == "message_out" and eng.obs["msg:m2"].subject == "Priya"
    assert "secret" not in json.dumps([o.to_dict() for o in eng.obs.values()])
    assert any(o.kind == "reminder" and o.subject == "Call mom" for o in eng.obs.values())
    assert "asks" in rt.errors                             # a failing source never stops the others


def test_behavior_created_reminders_are_not_ingested_back(tmp_path, store):
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    tick(rt, store, FakeHub())
    tick(rt, store, FakeHub(), now=NOW + br.INGEST_EVERY_S + 1)
    assert not any(o.subject == "Pack gym bag" for o in eng.obs.values())


def test_approval_and_denial_are_recorded(tmp_path, store):
    rt, eng = make_runtime(tmp_path)
    rt.record_decision("send_message", True)
    rt.record_decision("add_calendar_event", False)
    tick(rt, store, FakeHub())
    kinds = sorted((o.kind, o.subject) for o in eng.obs.values() if o.source == "wisp")
    assert kinds == [("approval", "send_message"), ("denial", "add_calendar_event")]


def test_agent_loop_hook_is_safe(monkeypatch, tmp_path):
    from service.agent.loop import _note_decision
    assert _note_decision("send_message", True) is True       # disabled: pass-through
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    rt, eng = make_runtime(tmp_path)
    br.set_runtime(rt)
    assert _note_decision("send_email", False) is False
    assert [o.kind for o in rt._pending] == ["denial"]


def test_published_auto_flags_are_true_only_for_performed_actions(tmp_path, store):
    store.add_manual("Pack gym bag", at(21, 0))              # action 0 is already on file
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    hub = FakeHub()
    tick(rt, store, hub)
    ev = next(e["event"] for e in hub.events if e["event"]["type"] == "nudge")
    assert [a["auto"] for a in ev["actions"]] == [False, True, False]
    assert [p["action_index"] for p in ev["auto_performed"]] == [1] and ev["can_undo"] is True
    [item] = rt.delivered()
    assert [a["auto"] for a in item["actions"]] == [False, True, False]
    rt.undo(store, "n1")
    assert [a["auto"] for a in rt.delivered()[0]["actions"]] == [False, False, False]


def test_forced_run_waits_for_a_running_tick_and_reports_busy(tmp_path, store, monkeypatch):
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    hub = FakeHub()

    async def scenario():
        rt._tick_lock = asyncio.Lock()
        await rt._tick_lock.acquire()                       # a scheduled tick is mid-flight
        assert await rt.tick(store, hub) == []              # a scheduled tick still skips
        forced = asyncio.ensure_future(rt.tick(store, hub, force=True))
        await asyncio.sleep(0.05)
        assert not forced.done()                            # waits instead of returning []
        rt._tick_lock.release()
        return await forced
    [row] = asyncio.run(scenario())
    assert row["nudge_id"] == "n1"

    monkeypatch.setattr(br, "FORCE_WAIT_S", 0.05)

    async def stuck():
        rt._tick_lock = asyncio.Lock()
        await rt._tick_lock.acquire()
        with pytest.raises(br.TickBusy):
            await rt.tick(store, hub, force=True)
    asyncio.run(stuck())


def test_scheduler_runs_behavior_tick_in_background_one_at_a_time(monkeypatch):
    from service.assistant import scheduler
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    started = []

    async def slow_tick(store, hub, **kw):
        started.append(1)
        await asyncio.sleep(0.05)
        return []
    monkeypatch.setattr(br, "tick", slow_tick)
    monkeypatch.setattr(scheduler, "_behavior_task", None)

    async def scenario():
        first = scheduler._start_behavior_tick()
        assert first is not None and not first.done()       # returned without awaiting the tick
        assert scheduler._start_behavior_tick() is None       # one at a time
        await first
        second = scheduler._start_behavior_tick()
        assert second is not None
        await second
    asyncio.run(scenario())
    assert len(started) == 2
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    ticks_before = len(started)

    async def off():
        task = scheduler._start_behavior_tick()
        assert task is not None                                # off: only retention runs ...
        await task
    asyncio.run(off())
    assert len(started) == ticks_before                        # ... never a tick (no engine, no runtime)


# --------------------------------------------------------------------------
# Profile card + prompt assembly
# --------------------------------------------------------------------------

def test_profile_card_cached_and_bounded(tmp_path):
    eng = FakeEngine()
    eng.card = "\n".join(f"- pattern line number {i} " + "x" * 40 for i in range(40))
    rt, _ = make_runtime(tmp_path, eng)
    card = rt.refresh_card()
    assert len(card.encode()) <= 700 and card.startswith("- pattern line number 0")
    eng.card = "changed"
    assert rt.profile_card() == card                    # cached for 60s


def test_profile_block_in_memory_assembly(monkeypatch, tmp_path):
    from service.memory import prompt_blocks
    monkeypatch.setattr("service.memory.facts.memory_context_block", lambda query="": "\nFACT", raising=False)
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    assert prompt_blocks.profile_block() == ""
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    rt, eng = make_runtime(tmp_path)
    rt.refresh_card()
    br.set_runtime(rt)
    block = prompt_blocks.profile_block()
    assert block.startswith(prompt_blocks.PROFILE_HEADER) and "Gym weekday mornings" in block
    assert "FACT" not in block                          # the card alone, without remembered facts
    assert prompt_blocks.memory_block("x").endswith(block)
    # The card fits a remote profile's byte budget alongside a system prompt.
    from tests.fixtures.remote_inference import example_profile
    example_profile().validate_payload({
        "model": "example-model", "stream": True, "max_tokens": 512,
        "messages": [{"role": "system", "content": "You are Wisp." + block},
                     {"role": "user", "content": "plan my morning"}]})


def test_main_has_no_remote_only_behavior_special_cases():
    """Remote inference follows the Mac path: the scheduler runs the behavior tick and the
    profile card rides in memory_block; main.py has no separate remote-only wiring."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "service/main.py").read_text()
    assert "remote_only_context_block" not in src and "behavior_runtime.run_loop" not in src
    assert "behavior_router" in src


# --------------------------------------------------------------------------
# Phraser adapter
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, content, *, managed=True, loaded=("fast-model",)):
        self.content, self.managed, self.loaded, self.calls = content, managed, loaded, []

    async def loaded_models(self):
        return list(self.loaded)

    async def chat(self, model, messages, **kw):
        self.calls.append((model, messages, kw))
        return {"choices": [{"message": {"content": self.content}, "finish_reason": "stop"}]}

    async def aclose(self):
        pass


def _target(monkeypatch, model="fast-model"):
    from types import SimpleNamespace
    target = SimpleNamespace(model=model)
    monkeypatch.setattr("service.config.endpoints.role_target", lambda role: target)
    return target


def test_phraser_rewrites_and_rejects_new_facts(monkeypatch):
    from service.assistant.behavior_phraser import WispPhraser
    from service import idle
    _target(monkeypatch)
    monkeypatch.setattr("service.inference.remote_policy.remote_only_profile", lambda: None)
    monkeypatch.setattr("service.config.no_thinking_kwargs", lambda model: {})
    client = FakeClient("You usually hit the gym around 6:30 — want me to protect that slot?")
    ph = WispPhraser(client_factory=lambda t: client)
    facts = {"habit": "gym", "time": "6:30"}
    assert ph.phrase("habit_conflict", facts) == client.content
    client.content = "Your 7:15 call with Dana conflicts."          # invents a time and a person
    assert ph.phrase("habit_conflict", facts) is None
    idle.begin_foreground()
    try:
        assert ph.phrase("habit_conflict", facts) is None             # never during foreground work
    finally:
        idle.end_foreground()
    cold = WispPhraser(client_factory=lambda t: FakeClient("ok", loaded=()))
    assert cold.phrase("x", facts) is None                            # never cold-loads a model


def test_phraser_categorize_and_failure(monkeypatch):
    from service.assistant.behavior_phraser import WispPhraser
    _target(monkeypatch)
    monkeypatch.setattr("service.inference.remote_policy.remote_only_profile", lambda: None)
    monkeypatch.setattr("service.config.no_thinking_kwargs", lambda model: {})
    ph = WispPhraser(client_factory=lambda t: FakeClient('{"1": "fitness", "2": "bogus"}'))
    assert ph.categorize(["Orangetheory", "Thing"], ("fitness", "other")) == {"Orangetheory": "fitness"}

    class Boom(FakeClient):
        async def chat(self, *a, **k):
            raise RuntimeError("down")
    assert WispPhraser(client_factory=lambda t: Boom("")).phrase("x", {"a": 1}) is None


def test_phraser_remote_only_payload_satisfies_remote_profile(monkeypatch):
    from service.assistant.behavior_phraser import WispPhraser
    from tests.fixtures.remote_inference import example_profile
    profile = example_profile()
    _target(monkeypatch, profile.model_id)
    monkeypatch.setattr("service.inference.remote_policy.remote_only_profile", lambda: profile)
    client = FakeClient("You protect your gym mornings.", managed=False, loaded=())
    ph = WispPhraser(client_factory=lambda t: client)
    assert ph.phrase("day_preview", {"habit": "gym mornings"}) == "You protect your gym mornings."
    model, messages, kw = client.calls[0]
    assert set(kw) == {"temperature", "max_tokens"}                  # no template kwargs on a remote profile
    profile.validate_payload({"model": model, "messages": messages, "stream": False, **kw})


def test_phraser_returns_none_on_event_loop():
    from service.assistant.behavior_phraser import WispPhraser

    async def inside():
        return WispPhraser(client_factory=lambda t: FakeClient("hi")).phrase("x", {"a": "hi"})
    assert asyncio.run(inside()) is None


# --------------------------------------------------------------------------
# Swift app contract (source checks; there is no Swift toolchain in CI)
# --------------------------------------------------------------------------

def _swift(name):
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "app/Sources/WispApp" / name).read_text()


def test_swift_nudge_trusts_can_undo_and_auto_performed():
    src = _swift("NudgeViews.swift")
    assert 'payload["auto_performed"]' in src and 'row["action_index"]' in src
    assert 'payload["can_undo"]' in src and "backendCanUndo ?? hasAuto" in src
    assert 'nudge.eventID = payload["event_id"]' in src and 'info["event_id"] = eventID' in src


def test_swift_refired_nudge_resets_card_and_banner():
    model = _swift("OverlayModel.swift")
    assert "nudge.eventID != old.eventID" in model
    notes = _swift("Notifications.swift")
    assert 'old.request.content.userInfo["event_id"]' in notes
    assert "center.removeDeliveredNotifications(withIdentifiers: [id])" in notes
    # Swift 6: the Task captures an immutable copy of the userInfo dictionary.
    assert "let info = info" in notes.split("didReceive response")[1]


def test_swift_learned_view_covers_every_pattern_kind_and_status():
    """The Memory section's Routines tab (formerly "What Wisp has learned") maps each engine pattern kind to a
    section and only sends statuses the API accepts, through the safe-id guard."""
    import re
    from service.behavior.types import PATTERN_KINDS
    from service.behavior_api import _PATTERN_STATUSES
    src = _swift("LearnedView.swift")
    mapping = src.split("static func of(_ kind: String)")[1].split("default:")[0]
    for kind in PATTERN_KINDS:
        assert f'"{kind}"' in mapping, kind
    sent = set(re.findall(r'model\.set\(p, to: "(\w+)"\)', src))
    assert sent and sent <= set(_PATTERN_STATUSES)
    client = _swift("WispClient.swift")
    setter = client.split("func setBehaviorPatternStatus")[1].split("}")[0]
    assert "behaviorSafeID(patternID)" in setter
    assert "case .routines: LearnedView(model: navigation.learned)" in _swift("MemoryHomeView.swift")
    assert "WispRouter.shared.open(.memory(.routines))" in _swift("SettingsView.swift")
    from service.behavior.predict import _USAGE_PHRASES
    table = src.split("static let usagePhrases")[1].split("= [", 1)[1].split("]")[0]
    assert dict(re.findall(r'"(\w+)": "([^"]+)"', table)) == _USAGE_PHRASES


def _feed_line(item):
    return "V3 | " + json.dumps(item, separators=(",", ":"))


def _feed_record(guid, conversation, sender, direction, ts):
    return {"version": 1, "kind": "record",
            "record": {"guid": guid, "conversation": conversation, "sender": sender,
                       "direction": direction, "timestamp": ts, "text": "private body", "links": []},
            "source": {"kind": "messages", "message_guid": guid, "chat_guid": conversation,
                       "navigation_url": None},
            "coverage": {"text": "complete", "links": "complete"}}


def test_real_messages_feed_reaches_the_engine_as_one_subject_per_person(tmp_path, monkeypatch):
    """Regression: the structured feed sends RFC 3339 timestamps and chat GUIDs. float() on the
    timestamp raised for every record and the except-continue dropped all of them, so the engine
    saw 0 messages; outgoing messages also used the chat GUID ("any;-;+1...") as the person."""
    from service.tools import imessage_tools as M
    for name, value in (("_lines", ""), ("_available", False), ("_unavailable_reason", ""),
                        ("_sync_completed", False), ("_contacts", {M._norm_handle("+1 (555) 555-0100"): "Sam Lee"})):
        monkeypatch.setattr(M, name, value)
    monkeypatch.setattr(M.cache_store, "save", lambda *_: None)
    t = int(NOW)
    rows = [_feed_record("g1", "any;-;+15555550100", "+15555550100", "incoming", t - 3600),
            _feed_record("g2", "any;-;+15555550100", "", "outgoing", t - 1800),
            _feed_record("g3", "any;-;priya@example.invalid", "priya@example.invalid", "incoming", t - 900),
            _feed_record("g4", "any;+;chat123456", "+15555550100", "incoming", t - 600),
            _feed_record("g5", "any;+;chat123456", "", "outgoing", t - 300),
            _feed_record("g6", "any;-;+15555550100", "+15555550100", "incoming", t - br.HISTORY_S - 60),
            _feed_record("g7", "any;-;36726", "36726", "incoming", t - 200),
            _feed_record("g8", "SMS;-;AMAZON", "AMAZON", "incoming", t - 190),
            _feed_record("g9", "SMS;-;+18005550100", "+18005550100", "incoming", t - 180),
            _feed_record("g10", "iMessage;-;urn:biz:5f1a2b", "urn:biz:5f1a2b", "incoming", t - 170)]
    coverage = {"version": 1, "kind": "coverage", "attempted": len(rows), "emitted": len(rows),
                "skipped": 0, "truncated": 0, "limit": 2000, "byte_limit": 4000000,
                "window_days": 365, "row_limit": 20000, "reached_row_limit": False}
    M.cache_messages("\n".join([_feed_line(coverage)] + [_feed_line(r) for r in rows]), available=True)
    snapshot = M.structured_messages_snapshot()
    assert isinstance(snapshot["records"][0]["timestamp"], str)        # the shape that used to be dropped
    engine = FakeEngine()
    rt = br.BehaviorRuntime(home=tmp_path / "home", local_factory=lambda path: engine)
    assert rt._ingest_messages(NOW) is True
    got = sorted((round(o.ts), o.kind, o.subject) for o in engine.obs.values())
    assert got == [(t - 3600, "message_in", "Sam Lee"),
                   (t - 1800, "message_out", "Sam Lee"),                # same person as their texts
                   (t - 900, "message_in", "priya@example.invalid"),
                   (t - 600, "message_in", "Sam Lee")]                  # group: the sender counts
    assert all(k.startswith("msg:") for k in engine.obs)
    # g5 (outgoing to a group) has no single counterpart; g6 is older than the mining window;
    # g7-g10 are automated senders (short code, alphanumeric id, toll-free, business chat).
    assert "private body" not in json.dumps([o.to_dict() for o in engine.obs.values()])


def test_message_timestamps_accept_rfc3339_and_numbers():
    assert br._message_ts("2026-10-14T03:54:22.027827Z") == pytest.approx(1791950062.027827)
    assert br._message_ts(1791950062) == 1791950062.0
    assert br._message_ts("2026-10-14T03:54:22") is None              # no zone: ambiguous, skipped
    assert br._message_ts("not a time") is None and br._message_ts(None) is None


def test_drain_guard_refuses_only_caller_scopes_and_restores_after_exceptions():
    from service.publication_authority import (
        Authority, PublicationBusy, Witness, assert_drain_allowed,
        assert_mutation_allowed, publication, revocation,
    )
    authority = Authority("drain-guard-control")
    authority.publish()
    witness = Witness.combine(authority.token())
    assert_drain_allowed()
    class ControlledFailure(Exception):
        pass
    with pytest.raises(ControlledFailure):
        with revocation():
            assert_mutation_allowed()
            with pytest.raises(PublicationBusy):
                assert_drain_allowed()
            with pytest.raises(ControlledFailure):
                with revocation():
                    assert_mutation_allowed()
                    with pytest.raises(PublicationBusy):
                        assert_drain_allowed()
                    raise ControlledFailure()
            # An inner exception must not clear the caller's ownership.
            with pytest.raises(PublicationBusy):
                assert_drain_allowed()
            # Ordinary nested mutations remain supported.
            authority.publish()
            raise ControlledFailure()
    assert_drain_allowed()
    witness = Witness.combine(authority.token())
    with pytest.raises(ControlledFailure):
        with publication(witness):
            with pytest.raises(PublicationBusy):
                assert_drain_allowed()
            raise ControlledFailure()
    assert_drain_allowed()
    with revocation():
        authority.publish()
    assert_drain_allowed()


def test_drain_guard_is_thread_local_and_never_waits_for_another_threads_barrier():
    import threading
    from service.publication_authority import assert_drain_allowed, revocation
    completed,errors = threading.Event(),[]
    def check():
        try:
            assert_drain_allowed()
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()
    worker = threading.Thread(target=check,daemon=True)
    with revocation():
        worker.start()
        # The guard reads its own scope only; it must not acquire the barrier.
        assert completed.wait(2)
    worker.join(3)
    assert not worker.is_alive() and not errors


def test_counterpart_keeps_people_and_drops_automated_senders():
    person = lambda d, conv, sender="": br._counterpart({"direction": d, "conversation": conv, "sender": sender})
    assert person("incoming", "any;-;+15555550101", "+15555550101") == "+15555550101"
    assert person("incoming", "any;-;sam@example.invalid", "sam@example.invalid") == "sam@example.invalid"
    assert person("outgoing", "SMS;-;+44 20 7946 0958") == "+44 20 7946 0958"
    assert person("outgoing", "any;+;chat1234") == ""                 # group: no single counterpart
    assert person("outgoing", "chat1234") == ""                       # unknown chat shape
    for automated in ("36726", "+1 800 555 0100", "18885550100", "AMAZON", "Chase", "urn:biz:9f"):
        assert person("incoming", "any;-;" + automated, automated) == "", automated


def test_a_failing_contact_lookup_falls_back_to_the_handle(tmp_path):
    messages = {"state": "ready", "records": [
        {"identity": "m1", "timestamp": NOW - 60, "direction": "incoming", "sender": "+15555550101"}]}
    def boom(handle):
        raise RuntimeError("contacts unavailable")
    rt, eng = make_runtime(tmp_path, messages=messages, contact_name_fn=boom)
    assert rt._ingest_messages(NOW) is True
    assert eng.obs["msg:m1"].subject == "+15555550101"


@pytest.mark.parametrize("mode", ["enabled", "off"])
def test_observation_retention_keeps_only_valid_overlapping_calendar_intervals(tmp_path, monkeypatch, mode):
    from service.behavior.store import Store, OBSERVATION_RETENTION_S
    from service.behavior.types import Observation
    from service.assistant import behavior_runtime
    now = 1792051200.0
    cutoff = now-OBSERVATION_RETENTION_S
    home = tmp_path / mode
    store = Store(home / behavior_runtime.ENGINE_DB)
    rows = [Observation("end-before", "event", now-190*86400, end_ts=cutoff-1),
            Observation("end-at", "event", now-190*86400, end_ts=cutoff),
            Observation("end-after", "event", now-190*86400, end_ts=cutoff+1),
            Observation("end-now", "event", now-190*86400, end_ts=now),
            Observation("unknown-end", "event", now-190*86400),
            Observation("invalid-end", "event", now-190*86400, end_ts=float("inf")),
            Observation("old-message", "message_in", now-190*86400, end_ts=now),
            Observation("old-reminder", "reminder_done", now-190*86400, end_ts=now),
            Observation("point-at", "event", cutoff),
            Observation("recent-message", "message_in", cutoff+1)]
    try:
        store.upsert_observations(rows)
        if mode == "enabled":
            result = store.prune(now)
        else:
            def prohibited(*_args, **_kwargs):
                raise AssertionError("Off-state retention constructed a runtime")
            monkeypatch.setattr(behavior_runtime, "get_runtime", prohibited)
            result = behavior_runtime.age_out(home=home, now=now)
        assert result["observations"] == 5
        assert {r.id for r in store.observations()} == {"end-at", "end-after", "end-now", "point-at", "recent-message"}
    finally:
        store.close()


# --------------------------------------------------------------------------
# Delivery cursor vs. a replaced behavior.db
# --------------------------------------------------------------------------

def _real_engine_runtime(home, monkeypatch):
    """Real BehaviorEngine at ``home/behavior.db``; due for nudges, nothing else."""
    from service.behavior import nudges as nudges_mod
    from service.behavior.engine import BehaviorEngine
    monkeypatch.setattr(nudges_mod, "build_candidates", lambda inputs: [])
    rt = br.BehaviorRuntime(home, local_factory=lambda path: BehaviorEngine(path, clock=lambda: NOW, tz="UTC"),
                            calendar_sync_fn=lambda: {"available": False}, clock=lambda: NOW)
    rt._stamps["local"] = {"ingest": NOW, "mine": NOW, "nudge": 0}
    rt._availability_attempt = NOW
    rt._card = (NOW, "")
    monkeypatch.setattr(rt, "_refresh_availability", lambda now: None)
    return rt


def _pending(rt, store, nid):
    """Insert one engine nudge, then run the work pass that reads the cursor."""
    rt.local_engine()._store.insert_nudge(gym_conflict_nudge(nid))
    rt._stamps["local"]["nudge"] = 0
    nudges, key = rt.work(store, NOW)
    assert "engine" not in rt.errors
    return [n.id for n in nudges]


def _deliver_all(rt, ids):
    """What tick does after a delivery: advance the cursor past each seq."""
    for n in rt.local_engine().nudges_since(0, 500)[0]:
        if n.id in ids:
            rt.kv_set("cursor:local", str(max(n.seq, int(rt.kv_get("cursor:local", "0")))))


def _move_aside_on_next_open(home):
    import sqlite3
    db = sqlite3.connect(str(home / br.ENGINE_DB))
    with db:
        # An unreadable version is still moved aside (a newer one pauses learning instead).
        db.execute("UPDATE meta SET value='\"garbage\"' WHERE key='schema_version'")
    db.close()


def test_cursor_rebases_when_behavior_db_is_moved_aside(tmp_path, store, monkeypatch):
    home = tmp_path / "home"
    rt = _real_engine_runtime(home, monkeypatch)
    assert _pending(rt, store, "before-1") == ["before-1"]
    _deliver_all(rt, ["before-1"])
    assert rt.work(store, NOW)[0] == []          # intact store: nothing offered twice
    rt.close()

    _move_aside_on_next_open(home)
    rt = _real_engine_runtime(home, monkeypatch)
    try:
        # The fresh store hands out seq 1 again: same value as the old cursor,
        # so only the store identity can tell the two seq spaces apart.
        assert _pending(rt, store, "after-1") == ["after-1"]
        assert list(home.glob("behavior.db.schema-garbage-*.bak"))
        _deliver_all(rt, ["after-1"])
        assert _pending(rt, store, "after-2") == ["after-2"]
    finally:
        rt.close()


def test_unbound_cursor_above_max_seq_rebases_but_within_range_is_adopted(tmp_path, store, monkeypatch):
    # A cursor written before store ids existed: the live 2026-10-16 state
    # (cursor 5 against a fresh store whose max_seq is 0).
    stale = _real_engine_runtime(tmp_path / "stale", monkeypatch)
    try:
        stale.kv_set("cursor:local", "5")
        assert _pending(stale, store, "fresh-1") == ["fresh-1"]
        assert stale.kv_get("cursor:local") == "0"
        assert stale.kv_get("cursor_store:local") == stale.local_engine().nudge_seq_space()["store_id"]
    finally:
        stale.close()

    intact = _real_engine_runtime(tmp_path / "intact", monkeypatch)
    try:
        for nid in ("old-1", "old-2"):
            intact.local_engine()._store.insert_nudge(gym_conflict_nudge(nid))
        intact.kv_set("cursor:local", "2")
        assert _pending(intact, store, "new-3") == ["new-3"]   # old-1/old-2 not redelivered
        assert intact.kv_get("cursor:local") == "2"
    finally:
        intact.close()


def test_a_newer_behavior_db_pauses_the_runtime_and_keeps_the_file(tmp_path, store, monkeypatch):
    """Running an older Wisp against a newer file (a rollback) must not erase what was learned."""
    import sqlite3
    home = tmp_path / "home"
    rt = _real_engine_runtime(home, monkeypatch)
    assert _pending(rt, store, "before-1") == ["before-1"]
    rt.close()
    db = sqlite3.connect(str(home / br.ENGINE_DB))
    with db:
        db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    db.close()
    rt = _real_engine_runtime(home, monkeypatch)
    try:
        assert rt.work(store, NOW)[0] == []
        assert "newer version of Wisp" in rt.errors.get("engine", "")
        assert "newer version of Wisp" in rt.status()["engine"]["error"]
        assert not list(home.glob("behavior.db.schema-*.bak"))
    finally:
        rt.close()
