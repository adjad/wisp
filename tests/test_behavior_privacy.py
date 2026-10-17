"""Privacy controls around behavior learning: forgetting, retention while off, the safety
policy on automatic writes, remote-mode wording, a fail-closed settings file, and an in-flight
pass that must stand down when learning is switched off. Synthetic data only; every path is
under tmp_path and the repository-root conftest isolates WISP_HOME."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import behavior_api
from service.assistant import behavior_runtime as br
from service.behavior.types import Action, Nudge
from tests.test_behavior_runtime import (NOW, FakeEngine, FakeHub, at, gym_conflict_nudge, make_runtime,
    queued_delivery, _delivery_counts)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from service import config
    for key in ("WISP_BEHAVIOR", "WISP_QA_SEED", "WISP_BEHAVIOR_PHRASER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "cfg" / "config.yaml")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    br.set_runtime(None)
    monkeypatch.setattr(br, "_last_age_out", None)
    yield
    br.set_runtime(None)
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


@pytest.fixture
def store(tmp_path):
    from service.assistant.store import AssistantStore
    return AssistantStore(tmp_path / "assistant.db")


@pytest.fixture
def writes_allowed():
    """Writes allowed (not View Only, not Full Access) so only the code under test decides."""
    from service.safety import policy
    before = (policy.read_only(), policy.full_access())
    policy.set_read_only(False)
    policy.set_full_access(False)
    yield policy
    policy.set_read_only(before[0])
    policy.set_full_access(before[1])


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(behavior_api.router)
    return TestClient(app)


def hold_files(home):
    """Stand-ins for everything the engine would have written, WAL and SHM included."""
    for name in ("behavior.db", "behavior.db-wal", "behavior.db-shm", "behavior_runtime.db-wal",
                 "behavior_runtime.db-shm", "behavior_profile_card.txt"):
        (home / name).write_text("learned")


# ----------------------------------------------------------------- M1: forget what was learned

def test_forget_deletes_files_connections_and_untouched_auto_items(tmp_path, store, writes_allowed):
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    br.set_runtime(rt)
    hub = FakeHub()
    asyncio.run(rt.tick(store, hub, now=NOW))
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2
    hold_files(rt.home)
    mine = store.add_manual("My own reminder", at(20, 0))
    # A durable nudge event for the learned nudge sits in the hub queue.
    persisted = store.event_by_key("behavior:n1")
    assert persisted is not None and persisted["kind"]=="nudge" and persisted["state"]=="pending"
    assert persisted["payload"]["nudge_id"]=="n1" and persisted["payload"]["can_undo"]
    with store.transaction() as db:
        db.execute("INSERT INTO assistant_events(id,kind,payload,dedupe_key,target,created_at) "
                   "VALUES ('e2','changed','{}','other:1','all',1)")

    result = br.forget_all(store)

    assert sorted(i["title"] for i in result["removed_items"]) == ["Pack gym bag", "Tentative: Gym"]
    assert result["kept_items"] == 0 and result["nudge_events_deleted"] == 1
    assert [r["title"] for r in store._db.execute("SELECT title FROM commitments")] == ["My own reminder"]
    assert store.get(mine["id"]) is not None
    assert store.event(persisted["id"]) is None
    assert store._db.execute("SELECT id FROM assistant_events").fetchall()[0][0] == "e2"
    assert sorted(p.name for p in rt.home.iterdir()) == []
    assert br._runtime is None and br.get_runtime() is not rt
    with pytest.raises(sqlite3.ProgrammingError):          # the connection really is closed
        rt._db.execute("SELECT 1")
    with pytest.raises(RuntimeError):                      # and the old object refuses to reopen the engine
        rt.local_engine()


def test_forget_keeps_items_the_user_changed(tmp_path, store, writes_allowed):
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    br.set_runtime(rt)
    asyncio.run(rt.tick(store, FakeHub(), now=NOW))
    rows = {r["title"]: r["id"] for r in store._db.execute("SELECT id, title FROM commitments")}
    store.update_schedule([rows["Pack gym bag"]], at(22, 0))            # moved by the user
    result = br.forget_all(store)
    assert [i["title"] for i in result["removed_items"]] == ["Tentative: Gym"] and result["kept_items"] == 1
    assert [r["title"] for r in store._db.execute("SELECT title FROM commitments")] == ["Pack gym bag"]


def test_forget_keeps_completed_and_already_deleted_items(tmp_path, store, writes_allowed):
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    br.set_runtime(rt)
    asyncio.run(rt.tick(store, FakeHub(), now=NOW))
    rows = {r["title"]: r["id"] for r in store._db.execute("SELECT id, title FROM commitments")}
    with store.transaction() as db:
        db.execute("UPDATE commitments SET status='done', updated_at=updated_at+5 WHERE id=?",
                   (rows["Pack gym bag"],))
    store.delete(rows["Tentative: Gym"])
    result = br.forget_all(store)
    assert result["removed_items"] == []
    assert [r["title"] for r in store._db.execute("SELECT title FROM commitments")] == ["Pack gym bag"]


def test_forget_works_while_learning_is_off(tmp_path, store, monkeypatch, writes_allowed):
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    br.set_runtime(rt)
    asyncio.run(rt.tick(store, FakeHub(), now=NOW))
    home = rt.home
    br.set_runtime(None)                       # a later launch: no runtime object, files still on disk
    rt.close()
    hold_files(home)
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    result = br.forget_all(store, home)
    assert len(result["removed_items"]) == 2
    assert list(home.iterdir()) == []
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0


def test_forget_with_nothing_learned_is_a_no_op(tmp_path, store):
    home = tmp_path / "empty"
    home.mkdir()
    assert br.forget_all(store, home) == {"removed_items": [], "kept_items": 0, "files_deleted": 0,
                                          "files_failed": [], "nudge_events_deleted": 0}


def test_forget_waits_for_a_pass_in_flight_then_nothing_is_published(tmp_path, store, writes_allowed):
    """stop() takes the work lock, so a pass running on the worker thread finishes first; a tick
    that resumes afterwards publishes nothing."""
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    br.set_runtime(rt)
    hub = FakeHub()
    nudges, _key = rt.work(store, NOW)
    br.forget_all(store)
    assert rt.live() is False
    assert asyncio.run(rt.deliver(store, hub, nudges[0], NOW)) is None
    assert hub.events == []


def test_delete_endpoint_requires_json_and_confirm(client, tmp_path, store, monkeypatch, writes_allowed):
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    br.set_runtime(rt)
    hub = FakeHub()
    monkeypatch.setattr(behavior_api, "_store_hub", lambda: (store, hub))
    asyncio.run(rt.tick(store, hub, now=NOW))
    assert client.delete("/behavior/data").status_code == 415
    assert client.request("DELETE", "/behavior/data", content=b'{"confirm": true}',
                          headers={"content-type": "text/plain"}).status_code == 415
    for bad in ({}, {"confirm": "yes"}, {"confirm": False}, [True]):
        assert client.request("DELETE", "/behavior/data", json=bad).status_code == 422, bad
    assert client.request("DELETE", "/behavior/data", content=b"{nope",
                          headers={"content-type": "application/json"}).status_code == 422
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2     # nothing happened yet
    resp = client.request("DELETE", "/behavior/data", json={"confirm": True})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert len(resp.json()["removed_items"]) == 2 and list(rt.home.iterdir()) == []
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0
    assert {"type": "changed"} in [e["event"] for e in hub.events]


def test_delete_endpoint_works_while_learning_is_off(client, tmp_path, store, monkeypatch):
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    monkeypatch.setattr(behavior_api, "_store_hub", lambda: (store, FakeHub()))
    monkeypatch.setattr(br, "_home", lambda: tmp_path)
    (tmp_path / "behavior.db").write_text("x")
    assert client.request("DELETE", "/behavior/data", json={"confirm": True}).status_code == 200
    assert not (tmp_path / "behavior.db").exists()


# ----------------------------------------------------------------- M1: retention while off

def _seed_engine_db(home, now):
    from service.behavior.store import Store
    from service.behavior.types import Observation
    st = Store(home / "behavior.db")
    st.upsert_observations([
        Observation(id="old", kind="event", ts=now - 181 * 86400, subject="Old", source="calendar"),
        Observation(id="new", kind="event", ts=now - 179 * 86400, subject="New", source="calendar")])
    for nid, age in (("n_old", 31), ("n_new", 29)):
        st.insert_nudge(Nudge(id=nid, kind="habit_conflict", title="t", body="b", because="c",
                              created_at=now - age * 86400, expires_at=None))
    st.close()


def _ids(home, table, col="id"):
    conn = sqlite3.connect(str(home / "behavior.db"))
    try:
        return {r[0] for r in conn.execute(f"SELECT {col} FROM {table}")}
    finally:
        conn.close()


def test_age_out_applies_180_and_30_day_limits_without_a_runtime(tmp_path):
    now = time.time()
    _seed_engine_db(tmp_path, now)
    rt = br.BehaviorRuntime(tmp_path, local_factory=lambda p: FakeEngine())
    rt.remember_nudge(gym_conflict_nudge("d_old"))
    rt.remember_nudge(gym_conflict_nudge("d_new"))
    rt._db.execute("UPDATE nudges SET delivered_at=? WHERE id='d_old'", (now - 31 * 86400,))
    rt._db.commit()
    rt.close()
    out = br.age_out(tmp_path, now)
    assert out == {"observations": 1, "nudges": 1, "delivered": 1}
    assert _ids(tmp_path, "observations") == {"new"} and _ids(tmp_path, "nudges", "id") == {"n_new"}
    conn = sqlite3.connect(str(tmp_path / "behavior_runtime.db"))
    assert {r[0] for r in conn.execute("SELECT id FROM nudges")} == {"d_new"}
    conn.close()
    assert br.age_out(tmp_path / "nowhere", now) == {"observations": 0, "nudges": 0, "delivered": 0}


def test_age_out_leaves_an_unreadable_db_alone(tmp_path):
    (tmp_path / "behavior.db").write_bytes(b"not a database" * 100)
    assert br.age_out(tmp_path)["observations"] == 0
    assert (tmp_path / "behavior.db").read_bytes().startswith(b"not a database")


def test_scheduler_tick_ages_out_even_while_learning_is_off(tmp_path, store, monkeypatch):
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    monkeypatch.setattr(br, "_home", lambda: tmp_path)
    now = time.time()
    _seed_engine_db(tmp_path, now)
    assert asyncio.run(br.tick(store, FakeHub())) == []
    assert _ids(tmp_path, "observations") == {"new"}
    # Rate limited: a second tick inside the interval does not scan again.
    calls = []
    monkeypatch.setattr(br, "age_out", lambda *a, **k: calls.append(1) or {})
    asyncio.run(br.tick(store, FakeHub()))
    assert calls == []


def test_turning_learning_off_ages_out_now(client, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(behavior_api.behavior_runtime, "age_out", lambda *a, **k: calls.append(1) or {})
    monkeypatch.setattr(behavior_api.behavior_runtime, "poke", lambda *a: None)
    epoch = client.post("/behavior/settings/check").json()["control_epoch"]
    assert client.put("/behavior/settings", json={"enabled": True, "control_epoch": epoch}).status_code == 200
    assert calls == []
    assert client.put("/behavior/settings", json={"enabled": False}).status_code == 200
    assert calls == [1]


# ----------------------------------------------------------------- M2: the safety policy

def view_only(policy):
    policy.set_read_only(True)
    policy.set_full_access(False)


def test_view_only_downgrades_auto_actions_to_offers_and_still_nudges(tmp_path, store, writes_allowed):
    view_only(writes_allowed)
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    hub = FakeHub()
    [row] = asyncio.run(rt.tick(store, hub, now=NOW))
    assert row["event_id"]
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0      # no auto write
    ev = [e["event"] for e in hub.events if e["event"]["type"] == "nudge"][0]
    assert ev["auto_performed"] == [] and ev["can_undo"] is False
    assert [a["auto"] for a in ev["actions"]] == [False, False, False]                     # offers only
    assert [a["type"] for a in ev["actions"]] == ["add_reminder", "add_hold", "open_prompt"]
    assert {"type": "changed"} not in [e["event"] for e in hub.events]
    assert rt.actions_for("n1") == []


def test_tapped_offer_is_blocked_too_in_view_only(tmp_path, store, monkeypatch, writes_allowed):
    view_only(writes_allowed)
    offered = Nudge(id="n2", kind="day_preview", title="T", body="B", because="C", created_at=NOW,
                    expires_at=at(23, 0, 1),
                    actions=(Action("add_reminder", {"title": "Prep", "when_ts": at(20, 0)}, auto=False),))
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[offered]))
    br.set_runtime(rt)
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    hub = FakeHub()
    monkeypatch.setattr(behavior_api, "_store_hub", lambda: (store, hub))
    app = FastAPI()
    app.include_router(behavior_api.router)
    http = TestClient(app)
    asyncio.run(rt.tick(store, hub, now=NOW))
    out = http.post("/behavior/nudges/n2/act", json={"action_index": 0}).json()
    assert out["ok"] is False and "safety policy" in out["detail"]
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0
    writes_allowed.set_read_only(False)
    out = http.post("/behavior/nudges/n2/act", json={"action_index": 0}).json()
    assert out["ok"] is True and out["created"]["title"] == "Prep"


def test_a_confirm_or_deny_decision_is_a_refusal_and_is_audited(tmp_path, store, monkeypatch, writes_allowed):
    from service import safety
    from service.safety import Decision, Tier
    seen = []
    monkeypatch.setattr(safety, "decide", lambda cat, args, tool=None: (
        seen.append((cat, args, tool)) or Decision(Tier.CONFIRM, "needs a person")))
    audited = []
    monkeypatch.setattr(safety, "audit", lambda event, **f: audited.append((event, f)))
    rt, _ = make_runtime(tmp_path)
    action = gym_conflict_nudge().actions[0]
    assert rt.policy_block(action) == "needs a person"
    assert seen[0][0] == "assistant_write" and seen[0][2] == "add_reminder"
    assert audited[0][0] == "deny" and audited[0][1]["source"] == "behavior"
    assert rt.perform_action(store, "n1", 0, action, NOW) is None
    monkeypatch.setattr(safety, "decide", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "unavailable" in rt.policy_block(action)


def test_allowed_policy_still_performs_auto_actions(tmp_path, store, writes_allowed):
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    asyncio.run(rt.tick(store, FakeHub(), now=NOW))
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2


def test_remote_inference_refusal_of_the_tool_blocks_the_write(tmp_path, store, monkeypatch, writes_allowed):
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: object())
    monkeypatch.setattr(remote_policy, "remote_tool_allowed", lambda tool: False)
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    asyncio.run(rt.tick(store, FakeHub(), now=NOW))
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0


# ----------------------------------------------------------------- M3: remote-mode wording

def test_phraser_is_off_in_remote_mode_unless_explicitly_enabled(tmp_path, monkeypatch):
    from service import behavior_switch as flags
    from service.inference import remote_policy
    # Local Mac: on by default, off with WISP_BEHAVIOR_PHRASER=0.
    assert flags.remote_active() is False and flags.phraser_enabled() is True
    eng = br._default_local_factory(tmp_path / "a.db")
    assert eng._phraser is not None
    eng.close()
    monkeypatch.setenv("WISP_BEHAVIOR_PHRASER", "0")
    assert flags.phraser_enabled() is False
    monkeypatch.delenv("WISP_BEHAVIOR_PHRASER")
    # Remote profile pinned: off by default.
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: object())
    assert flags.remote_active() is True and flags.phraser_enabled() is False
    eng = br._default_local_factory(tmp_path / "b.db")
    assert eng._phraser is None
    eng.close()
    monkeypatch.setenv("WISP_BEHAVIOR_PHRASER", "yes")                  # only "1" opts in
    assert flags.phraser_enabled() is False
    monkeypatch.setenv("WISP_BEHAVIOR_PHRASER", "1")
    assert flags.phraser_enabled() is True
    eng = br._default_local_factory(tmp_path / "c.db")
    assert eng._phraser is not None
    eng.close()
    # An invalid remote profile counts as remote.
    monkeypatch.delenv("WISP_BEHAVIOR_PHRASER")
    monkeypatch.setattr(remote_policy, "remote_only_profile",
                        lambda: (_ for _ in ()).throw(RuntimeError("invalid")))
    assert flags.remote_active() is True and flags.phraser_enabled() is False


def test_settings_endpoint_reports_remote_mode(client, monkeypatch):
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: object())
    data = client.get("/behavior/settings").json()
    assert data["remote_active"] is True and data["phraser"] is False


# ----------------------------------------------------------------- L1: unreadable settings fail closed

@pytest.mark.parametrize("content", ["- a\n- b\n", "a: [unterminated\n", "just a string\n", "42\n"])
def test_unreadable_or_non_mapping_config_turns_learning_off(client, tmp_path, monkeypatch, content):
    from service import behavior_switch as flags, config
    path = config.USER_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    assert flags.setting_readable() is False and flags.setting_enabled() is False
    assert flags.enabled() is False and br.get_runtime() is None
    data = client.get("/behavior/settings").json()
    assert data["enabled"] is False and data["settings_readable"] is False
    # Other config still loads (the packaged defaults), so startup survives.
    assert isinstance(config.models_config(), dict)
    # PUT is a clear error, not a 500, and the file is not overwritten.
    for value in (True, False):
        epoch = client.post("/behavior/settings/check").json()["control_epoch"]
        resp = client.put("/behavior/settings", json={"enabled": value, "control_epoch": epoch})
        assert resp.status_code == 409 and "settings file" in resp.json()["detail"]
    assert path.read_text() == content
    # The launch override still wins, as it always has.
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    assert flags.enabled() is True


def test_missing_or_empty_config_still_defaults_on(tmp_path):
    from service import behavior_switch as flags, config
    assert flags.setting_readable() is True and flags.setting_enabled() is True
    config.USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    config.USER_CONFIG.write_text("")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    assert flags.setting_readable() is True and flags.enabled() is True


def test_config_check_that_raises_is_off(monkeypatch):
    from service import behavior_switch as flags, config
    monkeypatch.setattr(config, "get_behavior_enabled", lambda: (_ for _ in ()).throw(OSError("denied")))
    assert flags.setting_enabled() is False


# ----------------------------------------------------------------- L2: an in-flight pass after the switch

def test_switch_off_during_a_pass_publishes_and_performs_nothing(tmp_path, store, monkeypatch, writes_allowed):
    class FlipEngine(FakeEngine):
        flipped = False

        def generate_nudges(self, now=None):
            out = super().generate_nudges(now)
            if not self.flipped:
                self.flipped = True
                monkeypatch.setenv("WISP_BEHAVIOR", "0")    # the user turns it off mid-pass
            return out

    rt, _ = make_runtime(tmp_path, FlipEngine(nudges=[gym_conflict_nudge()]))
    br.set_runtime(rt)
    hub = FakeHub()
    assert asyncio.run(rt.tick(store, hub, now=NOW)) == []
    assert hub.events == []
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0
    assert rt.kv_get("cursor:local", "0") == "0"            # not skipped: delivered if turned back on
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    assert len(asyncio.run(rt.tick(store, hub, now=NOW + 120, force=True))) == 1
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2


def test_switch_off_right_before_the_write_stops_that_write(queued_delivery, store, monkeypatch):
    from service.config import set_behavior_enabled
    rt,engine,hub,nudge,source,_ = queued_delivery
    real = rt.policy_block
    checked = []
    assert rt._prepare_delivery(store,nudge,NOW) is not None

    def flip(action):
        result = real(action)
        checked.append(1)
        set_behavior_enabled(False)
        return result

    monkeypatch.setattr(rt, "policy_block", flip)
    async def run():
        queue = hub.subscribe()
        assert await rt.deliver(store,hub,nudge,NOW) is None
        assert checked and not rt.live()
        assert queue.empty() and _delivery_counts(rt,store)==(0,0,0,0,0)
        hub.unsubscribe(queue)
    asyncio.run(run())


@pytest.mark.parametrize("automatic",[False,True])
def test_switch_off_right_before_publish_publishes_nothing(queued_delivery, store,automatic):
    from dataclasses import replace
    from service.config import set_behavior_enabled
    from service.publication_authority import PublicationUnavailable
    rt,engine,hub,_,source,candidates = queued_delivery
    offer_only = Nudge(id="n3", kind="day_preview", title="T", body="B", because="C", created_at=NOW,
                       expires_at=at(23, 0, 1),
                       pattern_ids=("p_gym",),evidence=("cal:g:1",),
                       actions=gym_conflict_nudge().actions if automatic else
                       (Action("open_prompt", {"prompt": "hi"}, auto=False),))
    inputs = rt._prepare_nudge_inputs(store,NOW)
    candidates[:] = [offer_only]
    [actual] = engine.generate_nudges(NOW)
    rt._stage_queued_nudge(actual,inputs,NOW)
    async def run():
        queue = hub.subscribe()
        loop = hub.prepare_nudge_delivery(store)
        prepared = rt._prepare_delivery(store,actual,NOW)
        assert prepared is not None and prepared.witness.current()
        set_behavior_enabled(False)
        with pytest.raises(PublicationUnavailable):
            rt._commit_delivery(store,hub,prepared,loop)
        assert await rt.deliver(store,hub,actual,NOW) is None
        assert queue.empty() and _delivery_counts(rt,store)==(0,0,0,0,0)
        set_behavior_enabled(True)
        assert await rt.deliver(store,hub,actual,NOW) is None
        assert queue.empty() and _delivery_counts(rt,store)==(0,0,0,0,0)
        fresh_inputs = rt._prepare_nudge_inputs(store,NOW)
        candidates[:] = [replace(actual,id="fresh-after-restore",kind="cancel_likely")]
        [fresh] = engine.generate_nudges(NOW)
        rt._stage_queued_nudge(fresh,fresh_inputs,NOW)
        published = await rt.deliver(store,hub,fresh,NOW)
        assert published is not None and published["can_undo"] is automatic
        assert _delivery_counts(rt,store)==((2,1,1,2,1) if automatic else (0,0,1,0,1))
        hub.unsubscribe(queue)
    asyncio.run(run())


def test_raw_env_override_is_observed_during_preparation_without_claiming_setter_ordering(
        queued_delivery,store,monkeypatch):
    rt,engine,hub,nudge,source,_ = queued_delivery
    assert rt._prepare_delivery(store,nudge,NOW) is not None
    original = rt.policy_block
    checked = []
    def flip(action):
        result = original(action)
        checked.append(1)
        monkeypatch.setenv("WISP_BEHAVIOR","0")
        return result
    monkeypatch.setattr(rt,"policy_block",flip)
    async def run():
        queue = hub.subscribe()
        assert await rt.deliver(store,hub,nudge,NOW) is None
        assert checked and queue.empty() and _delivery_counts(rt,store)==(0,0,0,0,0)
        monkeypatch.setenv("WISP_BEHAVIOR","1")
        monkeypatch.setattr(rt,"policy_block",original)
        assert await rt.deliver(store,hub,nudge,NOW) is not None
        assert _delivery_counts(rt,store)==(2,1,1,2,1)
        hub.unsubscribe(queue)
    asyncio.run(run())


def test_a_pass_after_stop_does_no_work(tmp_path, store):
    rt, eng = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    rt.stop()
    assert rt.work(store, NOW, force=True) == ([], "local")
    assert eng.mined == 0 and eng.generated == 0


# ----------------------------------------------------------------- Forget vs. concurrent runtime users

def _forget_in_thread(store, **kw):
    out = {}
    t = threading.Thread(target=lambda: out.update(result=br.forget_all(store, **kw)), daemon=True)
    t.start()
    return t, out


def _wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_nothing_recreates_a_runtime_while_forget_is_blocked_in_stop(tmp_path, store, monkeypatch, client,
                                                                     writes_allowed):
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    monkeypatch.setattr(br, "_home", lambda: rt.home)
    monkeypatch.setattr(behavior_api, "_store_hub", lambda: (store, FakeHub()))
    br.set_runtime(rt)
    rt.kv_set("cursor:local", "42")
    rt.remember_nudge(gym_conflict_nudge())
    hold_files(rt.home)
    rt._work_lock.acquire()                  # a pass in flight: stop() blocks on it
    t, out = _forget_in_thread(store)
    try:
        assert _wait_for(lambda: rt._stopped), "forget never reached stop()"
        assert br.forgetting() is True
        # Everything that asks for the runtime gets None; none of it builds a new one on the old files.
        assert br.get_runtime() is None
        assert br.profile_card() == ""
        br.poke(store, FakeHub())
        br.record_decision("send_email", True)
        assert asyncio.run(br.tick(store, FakeHub())) == []
        from service.tools import behavior_tools
        assert behavior_tools._runtime() is None
        assert br._runtime is None
        status = client.get("/behavior/status")
        assert status.status_code == 200 and status.json()["erasing"] is True
        assert status.json()["enabled"] is False
        for path in ("/behavior/patterns", "/behavior/nudges", "/behavior/predict", "/behavior/profile"):
            resp = client.get(path)
            assert resp.status_code == 503 and resp.headers["retry-after"], path
        assert client.post("/behavior/run").status_code == 503
        # A holder of the stale runtime cannot act on it either.
        with pytest.raises(br.RuntimeStopped):
            rt.perform_action(store, "n1", 0, gym_conflict_nudge().actions[0], NOW)
        assert asyncio.run(rt.deliver(store, FakeHub(), gym_conflict_nudge(), NOW)) is None
        assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0
        assert t.is_alive()                  # still blocked: nothing above got past the flag
    finally:
        rt._work_lock.release()
    t.join(10)
    assert not t.is_alive() and out["result"]["files_failed"] == []
    assert br.forgetting() is False and br._runtime is None
    assert list(rt.home.iterdir()) == []
    # A fresh runtime starts cleanly on new files, with none of the forgotten state, and can write.
    fresh = br.get_runtime()
    assert fresh is not None and fresh is not rt
    assert fresh.kv_get("cursor:local") is None
    assert fresh._db.execute("SELECT COUNT(*) FROM nudges").fetchone()[0] == 0
    fresh.kv_set("x", "y")
    assert fresh.kv_get("x") == "y" and (rt.home / br.RUNTIME_DB).exists()
    assert client.get("/behavior/status").json().get("erasing") is None


def test_forget_waits_for_a_delivery_in_flight(queued_delivery, store, monkeypatch):
    """stop() waits for a delivery already past its checks, so its write and publish land
    (and are then erased) before the files go, and none can land afterwards."""
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    release = threading.Event()
    entered = threading.Event()
    attempted = threading.Event()
    original = store.commit_nudge_actions
    def held(lease,plan,owner):
        entered.set()
        assert release.wait(5)
        return original(lease,plan,owner)
    monkeypatch.setattr(store,"commit_nudge_actions",held)

    result = {}
    loop_thread = threading.Thread(
        target=lambda: result.update(row=asyncio.run(rt.deliver(store,hub,nudge,NOW))),
        daemon=True)
    loop_thread.start()
    assert entered.wait(5)
    out = {}
    def forget():
        attempted.set()
        out["result"] = br.forget_all(store)
    t = threading.Thread(target=forget,daemon=True)
    t.start()
    try:
        assert attempted.wait(2)
        time.sleep(.03)
        assert t.is_alive() and not rt._stopped, "forget must wait before acknowledging stop"
    finally:
        release.set()
        loop_thread.join(10)
        t.join(10)
    assert not t.is_alive() and result["row"] is not None
    assert out["result"]["nudge_events_deleted"]==1
    assert len(out["result"]["removed_items"])==2
    assert store.event_by_key("behavior:"+nudge.id) is None
    assert list(rt.home.iterdir()) == []
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0     # auto items removed too


def test_forget_before_real_prepared_delivery_commit_has_no_effects(queued_delivery,store):
    from service.publication_authority import PublicationUnavailable
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    async def run():
        queue = hub.subscribe()
        loop = hub.prepare_nudge_delivery(store)
        prepared = rt._prepare_delivery(store,nudge,NOW)
        assert prepared is not None and prepared.witness.current()
        result = br.forget_all(store)
        assert result["removed_items"]==[] and result["nudge_events_deleted"]==0
        with pytest.raises(PublicationUnavailable):
            rt._commit_delivery(store,hub,prepared,loop)
        assert await rt.deliver(store,hub,nudge,NOW) is None
        assert queue.empty() and list(rt.home.iterdir())==[]
        for table in ("commitments","calendar_event_ends","assistant_events"):
            assert store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]==0
        hub.unsubscribe(queue)
    asyncio.run(run())


@pytest.mark.parametrize("recovery",["immediate","source_and_config_off","restarted","runtime_db_missing",
                                    "runtime_db_corrupt","runtime_constructor_failed"])
@pytest.mark.parametrize("change",["none","title","end","completed","source","source_id","kind","context","when","created"])
def test_forget_cleans_store_committed_actions_after_runtime_ledger_abort_without_redelivery(
        queued_delivery,store,recovery,change,monkeypatch):
    from service.config import set_behavior_enabled
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    rt._db.execute("CREATE TEMP TRIGGER fail_behavior_ledger BEFORE INSERT ON actions "
                   "BEGIN SELECT RAISE(ABORT,'synthetic ledger abort'); END")
    async def run():
        queue = hub.subscribe()
        with pytest.raises(br.DeliveryIncomplete):
            await rt.deliver(store,hub,nudge,NOW)
        assert _delivery_counts(rt,store)==(2,1,0,0,0) and queue.empty()
        hub.unsubscribe(queue)
    asyncio.run(run())
    rt._db.execute("DROP TRIGGER fail_behavior_ledger")
    manual = store.add_manual("Unrelated manual item",NOW+1200)
    # A row that merely resembles Wisp text is never cleanup ownership.
    lookalike = store.add_manual("Pack gym bag",at(21,0)+60,context=br.BEHAVIOR_CONTEXT)
    with store.transaction() as db:
        if change=="title":
            db.execute("UPDATE commitments SET title='User edit',updated_at=? WHERE kind='event'",(NOW+1,))
        elif change=="end":
            # End-only edits must be protected even if another writer failed
            # to bump the parent row's updated_at timestamp.
            db.execute("UPDATE calendar_event_ends SET end_ts=end_ts+60")
        elif change=="completed":
            db.execute("UPDATE commitments SET status='completed',updated_at=? WHERE kind='reminder' AND id NOT IN (?,?)",
                       (NOW+1,manual["id"],lookalike["id"]))
        elif change=="source":
            db.execute("UPDATE commitments SET source='calendar' WHERE kind='event'")
        elif change=="source_id":
            db.execute("UPDATE commitments SET source_id='changed-source-id' WHERE kind='event'")
        elif change=="kind":
            db.execute("UPDATE commitments SET kind='reminder' WHERE kind='event'")
        elif change=="context":
            db.execute("UPDATE commitments SET context=context||' user edit' WHERE kind='event'")
        elif change=="when":
            db.execute("UPDATE commitments SET when_ts=when_ts+0.5 WHERE kind='event'")
        elif change=="created":
            db.execute("UPDATE commitments SET created_at=created_at+1,updated_at=updated_at+1 WHERE kind='event'")
    if recovery=="source_and_config_off":
        source.withdraw()
        set_behavior_enabled(False)
    elif recovery in {"restarted","runtime_db_missing","runtime_db_corrupt","runtime_constructor_failed"}:
        br.set_runtime(None)
        rt.close()
        if recovery=="runtime_db_missing":
            for suffix in ("","-wal","-shm"):
                (rt.home / (br.RUNTIME_DB+suffix)).unlink(missing_ok=True)
        elif recovery=="runtime_db_corrupt":
            (rt.home/br.RUNTIME_DB).write_bytes(b"synthetic corrupt runtime database")
        elif recovery=="runtime_constructor_failed":
            def unavailable(*args,**kwargs):
                raise OSError("synthetic unavailable runtime database")
            monkeypatch.setattr(br.BehaviorRuntime,"__init__",unavailable)
    def forbidden(*args,**kwargs):
        raise AssertionError("Forget attempted fresh source admission or redelivery")
    monkeypatch.setattr(br.BehaviorRuntime,"_prepare_delivery",forbidden)
    monkeypatch.setattr(br,"_default_local_factory",forbidden)
    expected = ["Pack gym bag","Tentative: Gym"] if change=="none" else (
        ["Tentative: Gym"] if change=="completed" else ["Pack gym bag"])
    if recovery in {"runtime_db_corrupt","runtime_constructor_failed"}:
        with pytest.raises(br.ForgetIncomplete,match="Legacy behavior cleanup incomplete") as incomplete:
            br.forget_all(store,home=rt.home)
        assert incomplete.value.legacy_cleanup=="unknown"
        assert sorted(item["title"] for item in incomplete.value.known_removed_items)==expected
        assert incomplete.value.known_kept_items==(0 if change=="none" else 1)
        assert (rt.home/br.RUNTIME_DB).exists()
    else:
        result = br.forget_all(store,home=rt.home)
        assert sorted(item["title"] for item in result["removed_items"])==expected
        assert result["kept_items"]==(0 if change=="none" else 1) and result["nudge_events_deleted"]==0
        assert list(rt.home.iterdir())==[]
    assert store.get(manual["id"]) is not None and store.get(lookalike["id"]) is not None
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==(2 if change=="none" else 3)
    assert store._db.execute("SELECT COUNT(*) FROM calendar_event_ends").fetchone()[0]==(0 if change in {"none","completed"} else 1)
    assert store._db.execute("SELECT COUNT(*) FROM assistant_events").fetchone()[0]==0
    receipts = store._db.execute("SELECT * FROM assistant_behavior_action_receipts").fetchall()
    assert len(receipts)==2 and all(row["receipt_state"]=="consumed" for row in receipts)
    assert all(row[key] is None for row in receipts for key in ("type","kind","title","context","when_ts","end_ts","created_at"))


def test_interrupted_forget_durably_excludes_preserved_item_from_legacy_cleanup(queued_delivery,store,monkeypatch):
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    hold = dict(store._db.execute("SELECT * FROM commitments WHERE kind='event'").fetchone())
    with store.transaction() as db:
        db.execute("UPDATE calendar_event_ends SET end_ts=end_ts+60 WHERE commitment_id=?",(hold["id"],))
    manual = store.add_manual("Unrelated manual",NOW+600)
    hit = []
    def interrupted(*args,**kwargs):
        hit.append(1)
        # The durable cleanup phase ran first: only the edited hold and the
        # unrelated manual item survive. Runtime's weaker historical matcher
        # has not run, and its ledger is still on disk for the retry.
        assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==2
        assert store.get(hold["id"]) is not None
        raise RuntimeError("synthetic interrupted Forget after owned cleanup")
    monkeypatch.setattr(rt,"remove_untouched_auto_items",interrupted)
    with pytest.raises(br.ForgetIncomplete,match="Legacy behavior cleanup incomplete") as incomplete:
        br.forget_all(store,home=rt.home)
    assert type(incomplete.value.__cause__) is RuntimeError
    assert str(incomplete.value.__cause__)=="synthetic interrupted Forget after owned cleanup"
    assert [item["title"] for item in incomplete.value.known_removed_items]==["Pack gym bag"]
    assert incomplete.value.known_kept_items==1 and incomplete.value.legacy_cleanup=="unknown"
    assert hit and (rt.home/br.RUNTIME_DB).exists()
    source.withdraw()
    def forbidden(*args,**kwargs):
        raise AssertionError("Cleanup recovery used a fresh inference grant")
    monkeypatch.setattr(br,"_default_local_factory",forbidden)
    result = br.forget_all(store,home=rt.home)
    assert result["removed_items"]==[] and result["kept_items"]==1
    assert store.get(hold["id"]) is not None and store.get(manual["id"]) is not None
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==2
    assert store._db.execute("SELECT end_ts FROM calendar_event_ends WHERE commitment_id=?",(hold["id"],)).fetchone()[0]==at(18,30,1)+60
    assert list(rt.home.iterdir())==[]


def test_forget_receipt_consumption_failure_is_explicit_and_preserves_retry_evidence(queued_delivery,store):
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    store._db.execute("CREATE TEMP TRIGGER fail_consumption BEFORE UPDATE ON assistant_behavior_action_receipts "
                      "BEGIN SELECT RAISE(ABORT,'synthetic consumption failure'); END")
    with pytest.raises(sqlite3.DatabaseError,match="consumption failure"):
        br.forget_all(store,home=rt.home)
    assert (rt.home/br.RUNTIME_DB).exists()
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==2
    assert store._db.execute("SELECT COUNT(*) FROM calendar_event_ends").fetchone()[0]==1
    assert store._db.execute("SELECT COUNT(*) FROM assistant_behavior_action_receipts WHERE receipt_state='active'").fetchone()[0]==2
    store._db.execute("DROP TRIGGER fail_consumption")
    result = br.forget_all(store,home=rt.home)
    assert len(result["removed_items"])==2 and result["kept_items"]==0
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==0
    assert store._db.execute("SELECT COUNT(*) FROM assistant_behavior_action_receipts WHERE receipt_state='consumed'").fetchone()[0]==2
    assert list(rt.home.iterdir())==[]


def test_forget_preserves_other_owner_even_if_legacy_ledger_was_copied(queued_delivery,store,tmp_path):
    from dataclasses import replace
    from service.assistant.hub import Hub
    from service.publication_authority import Witness
    rt,engine,hub,nudge,source,candidates = queued_delivery
    other = br.BehaviorRuntime(tmp_path/"other-home",local_factory=lambda path:engine,clock=lambda:NOW,
        nudge_sources_fn=lambda store,now:{"calendar":Witness.combine(source.token())})
    inputs = other._prepare_nudge_inputs(store,NOW)
    candidates[:] = [replace(nudge,id="other-owned",actions=(Action("add_reminder",
        {"title":"Other owner's item","when_ts":NOW+1800},auto=True),))]
    [foreign] = engine.generate_nudges(NOW)
    other._stage_queued_nudge(foreign,inputs,NOW)
    async def run():
        assert await rt.deliver(store,hub,nudge,NOW) is not None
        assert await other.deliver(store,Hub(store),foreign,NOW) is not None
    try:
        asyncio.run(run())
        copied = other._db.execute("SELECT * FROM actions WHERE nudge_id=?",(foreign.id,)).fetchone()
        # A restored old ledger cannot override the Store's stronger ownership.
        with rt._db:
            rt._db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?)",tuple(copied))
        br.set_runtime(rt)
        result = br.forget_all(store,home=rt.home)
        assert len(result["removed_items"])==2 and result["kept_items"]==0
        assert store.get(copied["commitment_id"])["title"]=="Other owner's item"
        receipt = store._db.execute("SELECT * FROM assistant_behavior_action_receipts WHERE owner_id=?",(other._owner_id,)).fetchone()
        assert receipt["receipt_state"]=="active" and receipt["title"]=="Other owner's item"
        assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==1
    finally:
        other.close()


def test_forget_wrong_explicit_home_refuses_before_stopping_or_mutating_active_home(queued_delivery,store,tmp_path):
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    legacy = rt.perform_action(store,"legacy-offer",0,Action("add_reminder",
        {"title":"Legacy owned item","when_ts":NOW+600},auto=False),NOW)
    assert legacy is not None
    before = _delivery_counts(rt,store)
    requested = tmp_path/"different-requested-home"
    requested.mkdir()
    sentinel = requested/br.CARD_CACHE
    sentinel.write_text("different profile state")
    with pytest.raises(ValueError,match="active behavior home"):
        br.forget_all(store,home=requested)
    assert rt.live() and br._runtime is rt and not br.forgetting()
    assert _delivery_counts(rt,store)==before
    assert store.get(legacy["commitment_id"]) is not None
    assert store.event_by_key("behavior:"+nudge.id)["state"]=="pending"
    assert sentinel.read_text()=="different profile state"
    assert (rt.home/br.RUNTIME_DB).exists()


def test_forget_canonical_alias_and_cold_default_home_use_same_owner(queued_delivery,store,tmp_path,monkeypatch):
    rt,engine,hub,nudge,source,_ = queued_delivery
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    alias = tmp_path/"home-alias"
    alias.symlink_to(rt.home,target_is_directory=True)
    br.set_runtime(rt)
    result = br.forget_all(store,home=alias)
    assert len(result["removed_items"])==2 and result["kept_items"]==0
    assert list(rt.home.iterdir())==[] and alias.is_symlink()
    monkeypatch.setattr(br,"_home",lambda:alias)
    # No runtime and no runtime DB: the same consumed identities remain
    # exclusions, with no extra item deletion or recreated runtime files.
    repeated = br.forget_all(store)
    assert repeated["removed_items"]==[] and repeated["kept_items"]==0
    assert list(rt.home.iterdir())==[]


def test_forget_tilde_uses_expanded_home_for_ownership_and_file_erasure(queued_delivery,store,tmp_path,monkeypatch):
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    literal = tmp_path/"~"/rt.home.name
    literal.mkdir(parents=True)
    sentinel = literal/br.CARD_CACHE
    sentinel.write_text("unrelated literal tilde directory")
    monkeypatch.setenv("HOME",str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = br.forget_all(store,home=Path("~")/rt.home.name)
    assert len(result["removed_items"])==2 and result["nudge_events_deleted"]==1
    assert list(rt.home.iterdir())==[]
    assert sentinel.read_text()=="unrelated literal tilde directory"


def test_forget_reentry_is_busy_before_home_metadata_and_private_locks(queued_delivery,store,monkeypatch):
    from service.publication_authority import PublicationBusy,publication
    from service.assistant import store as store_module
    rt,engine,hub,nudge,source,_ = queued_delivery
    prepared = rt._prepare_delivery(store,nudge,NOW)
    class ForbiddenLock:
        def __enter__(self):
            raise AssertionError("Forget reached private lock before Busy")
        def __exit__(self,*args):
            raise AssertionError("Unentered lock exited")
    def forbidden(*args,**kwargs):
        raise AssertionError("Forget reached filesystem metadata before Busy")
    with publication(prepared.witness,writes=(prepared.plan.binding,)):
        with monkeypatch.context() as patch:
            patch.setattr(Path,"resolve",forbidden)
            patch.setattr(store_module,"behavior_owner_id",forbidden)
            patch.setattr(br,"_forget_lock",ForbiddenLock())
            patch.setattr(br,"_runtime_lock",ForbiddenLock())
            with pytest.raises(PublicationBusy):
                br.forget_all(store,home=rt.home)
    assert _delivery_counts(rt,store)==(0,0,0,0,0)


@pytest.mark.parametrize("write",[True,False])
def test_forget_refuses_caller_store_transaction_before_withdrawing_runtime(queued_delivery,store,write):
    from service.publication_authority import PublicationBusy
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    before = _delivery_counts(rt,store)
    proof = rt._publication.token()
    class OuterRollback(Exception):
        pass
    with pytest.raises(OuterRollback):
        with store.transaction(write=write):
            if write:
                store.add_manual("Outer transaction item",NOW+1200)
            current = _delivery_counts(rt,store)
            # The supported Store context also owns revocation. Waiting
            # cleanup refuses that scope before touching even Store admission.
            with pytest.raises(PublicationBusy,match="caller publication or mutation"):
                br.forget_all(store,home=rt.home)
            assert store._db.in_transaction and proof.current()
            assert rt.live() and br._runtime is rt and not br.forgetting()
            assert _delivery_counts(rt,store)==current
            assert (rt.home/br.RUNTIME_DB).exists()
            raise OuterRollback()
    assert _delivery_counts(rt,store)==before


def test_receipt_cleanup_refuses_outer_transaction_before_consuming_or_deleting(queued_delivery,store):
    from service.publication_authority import PublicationUnavailable
    rt,engine,hub,nudge,source,_ = queued_delivery
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    before = _delivery_counts(rt,store)
    with store.transaction():
        with pytest.raises(PublicationUnavailable,match="caller transaction"):
            store.forget_behavior_actions(rt._owner_id)
        assert store._db.in_transaction and _delivery_counts(rt,store)==before
        assert store._db.execute("SELECT COUNT(*) FROM assistant_behavior_action_receipts WHERE receipt_state='active'").fetchone()[0]==2
        with pytest.raises(PublicationUnavailable,match="caller transaction"):
            store.drop_behavior_events()
        assert store.event_by_key("behavior:"+nudge.id)["state"]=="pending"


def test_cleanup_admission_checks_only_its_actual_write_connection(queued_delivery,store):
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    reader = sqlite3.connect(store.path)
    try:
        reader.execute("PRAGMA query_only=ON")
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM commitments").fetchone()
        assert reader.in_transaction and not store._db.in_transaction
        store.assert_behavior_cleanup_available()
    finally:
        reader.rollback()
        reader.close()
    assert len(br.forget_all(store,home=rt.home)["removed_items"])==2


def test_unavailable_cold_legacy_ledger_preserves_evidence_then_readable_retry_cleans_it(
        queued_delivery,store,monkeypatch):
    rt,engine,hub,nudge,source,_ = queued_delivery
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    legacy = rt.perform_action(store,"real-legacy",0,Action("add_reminder",
        {"title":"Real old unreceipted item","when_ts":NOW+600},auto=False),NOW)
    assert legacy is not None
    manual = store.add_manual("Unrelated manual",NOW+900)
    with store.transaction() as db:
        db.execute("UPDATE calendar_event_ends SET end_ts=end_ts+60")
    assert store._db.execute("SELECT COUNT(*) FROM assistant_behavior_action_receipts").fetchone()[0]==2
    rt.close()
    evidence = {rt.home/(br.RUNTIME_DB+suffix):(rt.home/(br.RUNTIME_DB+suffix)).read_bytes()
                for suffix in ("","-wal","-shm") if (rt.home/(br.RUNTIME_DB+suffix)).exists()}
    hit = []
    def unavailable(*args,**kwargs):
        hit.append(1)
        raise sqlite3.OperationalError("synthetic temporary ledger open failure")
    with monkeypatch.context() as patch:
        patch.setattr(br.BehaviorRuntime,"__init__",unavailable)
        with pytest.raises(br.ForgetIncomplete) as incomplete:
            br.forget_all(store,home=rt.home)
        assert hit and incomplete.value.legacy_cleanup=="unknown"
        assert [item["title"] for item in incomplete.value.known_removed_items]==["Pack gym bag"]
        assert incomplete.value.known_kept_items==1
    assert all(path.read_bytes()==data for path,data in evidence.items())
    assert store.get(legacy["commitment_id"]) is not None and store.get(manual["id"]) is not None
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==3
    assert store._db.execute("SELECT COUNT(*) FROM assistant_behavior_action_receipts WHERE receipt_state='consumed'").fetchone()[0]==2
    def forbidden(*args,**kwargs):
        raise AssertionError("Legacy cleanup attempted Engine/model/source admission")
    monkeypatch.setattr(br,"_default_local_factory",forbidden)
    source.withdraw()
    recovered = br.forget_all(store,home=rt.home)
    assert [item["title"] for item in recovered["removed_items"]]==["Real old unreceipted item"]
    assert recovered["kept_items"]==1 and recovered["nudge_events_deleted"]==1
    assert store.get(manual["id"]) is not None
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==2
    assert list(rt.home.iterdir())==[]


def test_active_legacy_ledger_read_failure_is_incomplete_and_retryable(queued_delivery,store):
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    legacy = rt.perform_action(store,"old-ledger-read",0,Action("add_reminder",
        {"title":"Old ledger item","when_ts":NOW+600},auto=False),NOW)
    assert legacy is not None
    rt._db.set_authorizer(lambda operation,table,*args:
        sqlite3.SQLITE_DENY if operation==sqlite3.SQLITE_READ and table=="actions" else sqlite3.SQLITE_OK)
    with pytest.raises(br.ForgetIncomplete) as incomplete:
        br.forget_all(store,home=rt.home)
    assert incomplete.value.legacy_cleanup=="unknown"
    assert len(incomplete.value.known_removed_items)==2
    assert (rt.home/br.RUNTIME_DB).exists()
    assert store.get(legacy["commitment_id"]) is not None
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==1
    # The failed runtime is closed. A fresh reader can use the retained valid
    # ledger without its old per-connection authorizer or any inference grant.
    recovered = br.forget_all(store,home=rt.home)
    assert [item["title"] for item in recovered["removed_items"]]==["Old ledger item"]
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==0
    assert list(rt.home.iterdir())==[]


def test_forget_own_reservation_reentry_is_busy_and_failure_wakes_waiters(queued_delivery,store,monkeypatch):
    from service.publication_authority import PublicationBusy
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    entered,release,waited = threading.Event(),threading.Event(),threading.Event()
    original = store.forget_behavior_actions
    wait = br._forget_done.wait
    calls,errors,results = [],[],[]
    def observed_wait(timeout=None):
        waited.set()
        if not wait(3 if timeout is None else min(timeout,3)):
            raise TimeoutError("Controlled Forget waiter did not wake")
        return True
    monkeypatch.setattr(br._forget_done,"wait",observed_wait)
    def held(owner):
        calls.append(1)
        if len(calls)==1:
            with pytest.raises(PublicationBusy,match="already owns Forget"):
                br.forget_all(store,home=rt.home)
            entered.set()
            assert release.wait(3)
            raise RuntimeError("synthetic first Forget failure")
        return original(owner)
    monkeypatch.setattr(store,"forget_behavior_actions",held)
    def erase():
        try:
            results.append(br.forget_all(store,home=rt.home))
        except BaseException as exc:
            errors.append(exc)
    first = threading.Thread(target=erase,daemon=True)
    second = threading.Thread(target=erase,daemon=True)
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert waited.wait(2) and br.get_runtime() is None
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert len(errors)==1 and str(errors[0])=="synthetic first Forget failure"
    assert len(results)==1 and len(results[0]["removed_items"])==2
    assert not br.forgetting() and br._forget_owner_thread is None and br._forget_done.is_set()
    assert list(rt.home.iterdir())==[]


def test_concurrent_forget_waits_outside_barrier_while_worker_finishes(queued_delivery,store,monkeypatch):
    from service.publication_authority import Authority,Witness,publication
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    independent = Authority("unrelated-test-publication")
    independent.publish()
    witness = Witness.combine(independent.token())
    waited,published = threading.Event(),threading.Event()
    original_wait = br._forget_done.wait
    errors,results = [],[]
    def wait(timeout=None):
        waited.set()
        assert not br._forget_lock.locked() and not br._runtime_lock.locked()
        with publication(witness):
            published.set()
        if not original_wait(3 if timeout is None else min(timeout,3)):
            raise TimeoutError("Controlled Forget waiter did not wake")
        return True
    monkeypatch.setattr(br._forget_done,"wait",wait)
    def erase():
        try:
            results.append(br.forget_all(store,home=rt.home))
        except BaseException as exc:
            errors.append(exc)
    rt._work_lock.acquire()
    first = threading.Thread(target=erase,daemon=True)
    second = threading.Thread(target=erase,daemon=True)
    first.start()
    try:
        assert _wait_for(lambda:rt._stopped)
        second.start()
        assert waited.wait(2) and published.wait(2)
        assert first.is_alive() and second.is_alive()
    finally:
        rt._work_lock.release()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive() and not errors and len(results)==2
    assert not br.forgetting() and br._forget_done.is_set()
    assert list(rt.home.iterdir())==[]


@pytest.mark.parametrize("scope",["bare","foreign_store"])
def test_forget_refuses_inherited_revocation_before_metadata_or_store_work(queued_delivery,store,monkeypatch,tmp_path,scope):
    from service.publication_authority import PublicationBusy,revocation
    from service.assistant.store import AssistantStore
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    def forbidden(*args,**kwargs):
        raise AssertionError("Scoped Forget reached metadata or Store admission")
    foreign = AssistantStore(tmp_path/"foreign.db") if scope=="foreign_store" else None
    try:
        with (foreign.transaction() if foreign is not None else revocation()):
            if foreign is not None:
                foreign.add_manual("Unrelated caller item",NOW+1200)
            with monkeypatch.context() as patch:
                patch.setattr(Path,"resolve",forbidden)
                patch.setattr(store,"assert_behavior_cleanup_available",forbidden)
                with pytest.raises(PublicationBusy):
                    br.forget_all(store,home=rt.home)
            if foreign is not None:
                assert foreign._db.in_transaction
                assert foreign._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==1
    finally:
        if foreign is not None:
            foreign._db.close()
    assert rt.live() and br._runtime is rt and not br.forgetting()
    assert _delivery_counts(rt,store)==(0,0,0,0,0)


def test_inherited_revoker_cannot_wait_on_another_forget_worker(queued_delivery,store,monkeypatch):
    from service.publication_authority import PublicationBusy,revocation
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    completed,waited = threading.Event(),threading.Event()
    original_wait = br._forget_done.wait
    errors,results = [],[]
    def bounded_wait(timeout=None):
        waited.set()
        if not original_wait(3 if timeout is None else min(timeout,3)):
            raise TimeoutError("Inherited revoker waited for its own barrier")
        return True
    monkeypatch.setattr(br._forget_done,"wait",bounded_wait)
    def first_erase():
        try:
            results.append(br.forget_all(store,home=rt.home))
        except BaseException as exc:
            errors.append(exc)
    def inherited():
        try:
            with revocation():
                br.forget_all(store,home=rt.home)
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()
    first = threading.Thread(target=first_erase,daemon=True)
    caller = threading.Thread(target=inherited,daemon=True)
    rt._work_lock.acquire()
    first.start()
    try:
        assert _wait_for(lambda:rt._stopped)
        caller.start()
        assert completed.wait(2)
        assert len(errors)==1 and isinstance(errors[0],PublicationBusy)
        assert not waited.is_set() and first.is_alive()
        assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]==2
    finally:
        rt._work_lock.release()
        first.join(6)
        if caller.ident is not None:
            caller.join(6)
    assert not first.is_alive() and not caller.is_alive() and len(results)==1
    assert len(results[0]["removed_items"])==2
    assert not br.forgetting() and br._forget_done.is_set()


@pytest.mark.parametrize("write",[True,False])
def test_forget_refuses_raw_caller_transaction_without_inherited_revocation(queued_delivery,store,write):
    from service.publication_authority import PublicationUnavailable
    rt,engine,hub,nudge,source,_ = queued_delivery
    br.set_runtime(rt)
    assert asyncio.run(rt.deliver(store,hub,nudge,NOW)) is not None
    before = _delivery_counts(rt,store)
    proof = rt._publication.token()
    store._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        if write:
            store._db.execute("UPDATE commitments SET title='Caller edit' WHERE kind='reminder'")
        current = _delivery_counts(rt,store)
        with pytest.raises(PublicationUnavailable,match="caller transaction"):
            br.forget_all(store,home=rt.home)
        assert store._db.in_transaction and proof.current()
        assert _delivery_counts(rt,store)==current and rt.live() and br._runtime is rt
        assert (rt.home/br.RUNTIME_DB).exists()
    finally:
        store._db.rollback()
    assert _delivery_counts(rt,store)==before and not br.forgetting()


def test_a_stale_act_request_gets_a_clean_refusal_not_a_closed_database_500(tmp_path, store, monkeypatch, client,
                                                                          writes_allowed):
    rt, _ = make_runtime(tmp_path)
    offer = Nudge(id="n9", kind="day_preview", title="T", body="B", because="C", created_at=NOW,
                  expires_at=at(23, 0, 1),
                  actions=(Action("add_reminder", {"title": "Pack bag", "when_ts": at(21, 0)}, auto=False),))
    rt.remember_nudge(offer)
    monkeypatch.setattr(behavior_api.behavior_runtime, "get_runtime", lambda: rt)   # the request already holds it
    monkeypatch.setattr(behavior_api, "_store_hub", lambda: (store, FakeHub()))
    rt.stop()
    with pytest.raises(br.RuntimeStopped):
        rt.perform_action(store, "n9", 0, offer.actions[0], NOW)
    resp = client.post("/behavior/nudges/n9/act", json={"action_index": 0})
    assert resp.status_code == 409 and "erased" in resp.json()["detail"]
    rt.close()                                                  # and after the files are gone too
    for call in (lambda: client.post("/behavior/nudges/n9/act", json={"action_index": 0}),
                 lambda: client.post("/behavior/nudges/n9/undo"),
                 lambda: client.post("/behavior/nudges/n9/feedback", json={"verdict": "useful"}),
                 lambda: client.get("/behavior/nudges")):
        assert call().status_code == 409
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0


def test_concurrent_forgets_serialise_and_clear_the_flag_only_at_the_end(tmp_path, store):
    rt, _ = make_runtime(tmp_path)
    br.set_runtime(rt)
    rt._work_lock.acquire()
    first, _o1 = _forget_in_thread(store)
    assert _wait_for(lambda: rt._stopped)
    second, _o2 = _forget_in_thread(store)
    time.sleep(0.2)
    rt._work_lock.release()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert br.forgetting() is False


# ----------------------------------------------------------------- Forget with files that cannot be deleted

def test_forget_reports_files_it_could_not_delete_and_deletes_the_rest(tmp_path, store, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    hold_files(home)
    real = Path.unlink

    def flaky(self, *a, **k):
        if self.name == "behavior.db-wal":
            raise PermissionError(13, "Operation not permitted")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky)
    result = br.forget_all(store, home)
    assert result["files_failed"] == ["behavior.db-wal"]
    assert sorted(p.name for p in home.iterdir()) == ["behavior.db-wal"]       # everything else is gone
    assert result["files_deleted"] == 5 and br.forgetting() is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_forget_with_a_read_only_directory_answers_200_listing_what_is_left(client, tmp_path, store, monkeypatch):
    home = tmp_path / "ro"
    home.mkdir()
    hold_files(home)
    monkeypatch.setattr(br, "_home", lambda: home)
    monkeypatch.setattr(behavior_api, "_store_hub", lambda: (store, FakeHub()))
    home.chmod(0o500)
    try:
        resp = client.request("DELETE", "/behavior/data", json={"confirm": True})
    finally:
        home.chmod(0o700)
    body = resp.json()
    assert resp.status_code == 200 and body["ok"] is False
    assert "behavior_profile_card.txt" in body["files_failed"] and body["files_deleted"] == 0
    assert br.forgetting() is False


# ----------------------------------------------------------------- retention while off, through the scheduler

def _scheduler_off_case(tmp_path, monkeypatch):
    from service.assistant import scheduler
    monkeypatch.setattr(br, "_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "_behavior_task", None)
    now = time.time()
    _seed_engine_db(tmp_path, now)

    async def go():
        task = scheduler._start_behavior_tick()
        assert task is not None
        await task
        await scheduler._start_behavior_tick()           # inside the interval: runs, but does not rescan

    scans = []
    real = br.age_out
    monkeypatch.setattr(br, "age_out", lambda *a, **k: scans.append(1) or real(*a, **k))
    asyncio.run(go())
    assert _ids(tmp_path, "observations") == {"new"} and _ids(tmp_path, "nudges") == {"n_new"}
    assert len(scans) == 1
    assert br._runtime is None and not (tmp_path / br.RUNTIME_DB).exists()    # no runtime, engine or phraser


def test_scheduler_ages_out_while_learning_is_off_by_the_launch_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    _scheduler_off_case(tmp_path, monkeypatch)


def test_scheduler_ages_out_while_the_saved_setting_is_off(tmp_path, monkeypatch):
    from service.config import set_behavior_enabled
    set_behavior_enabled(False)
    _scheduler_off_case(tmp_path, monkeypatch)


def test_scheduler_ages_out_when_the_settings_file_is_corrupt(tmp_path, monkeypatch):
    from service import config
    config.USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    config.USER_CONFIG.write_text("behavior: [unclosed\n  : :")
    config._user_overlay.cache_clear()
    from service.behavior_switch import enabled
    assert enabled() is False                                  # fail closed
    _scheduler_off_case(tmp_path, monkeypatch)


def test_scheduler_does_not_age_out_mid_erase(tmp_path, monkeypatch):
    from service.assistant import scheduler
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    monkeypatch.setattr(br, "_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "_behavior_task", None)
    scans = []
    monkeypatch.setattr(br, "age_out", lambda *a, **k: scans.append(1) or {})
    monkeypatch.setattr(br, "_forgetting", True)

    async def go():
        await scheduler._start_behavior_tick()
    asyncio.run(go())
    assert scans == []


def test_age_out_never_creates_a_database_that_is_not_there(tmp_path):
    assert br.age_out(tmp_path) == {"observations": 0, "nudges": 0, "delivered": 0}
    assert list(tmp_path.iterdir()) == []


def test_age_out_leaves_a_newer_engine_db_byte_for_byte(tmp_path):
    """A newer Wisp's file is that build's to age out under its own rules."""
    from service.behavior.store import SCHEMA_VERSION
    now = time.time()
    _seed_engine_db(tmp_path, now)
    conn = sqlite3.connect(str(tmp_path / "behavior.db"))
    with conn:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = (tmp_path / "behavior.db").read_bytes()
    assert br.age_out(tmp_path, now)["observations"] == 0
    assert (tmp_path / "behavior.db").read_bytes() == before


def test_retention_fails_closed_when_the_version_cannot_be_read(tmp_path, monkeypatch):
    """A newer process holding a lock must not make retention apply old rules to its file."""
    from service.behavior import store as store_mod
    now = time.time()
    _seed_engine_db(tmp_path, now)
    real = sqlite3.connect

    def locked(target, *a, **kw):
        if "mode=ro" in str(target):
            raise sqlite3.OperationalError("database is locked")
        return real(target, *a, **kw)
    monkeypatch.setattr(store_mod.sqlite3, "connect", locked)
    assert store_mod.written_by_newer_build(tmp_path / "behavior.db") is True
    monkeypatch.setattr(store_mod.sqlite3, "connect", real)
    assert store_mod.written_by_newer_build(tmp_path / "behavior.db") is False
    assert store_mod.written_by_newer_build(tmp_path / "missing.db") is False
