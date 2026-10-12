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
from tests.test_behavior_runtime import NOW, FakeEngine, FakeHub, at, gym_conflict_nudge, make_runtime


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
    with store.transaction() as db:
        db.execute("INSERT INTO assistant_events(id,kind,payload,dedupe_key,target,created_at) "
                   "VALUES ('e1','nudge','{}','behavior:n1','all',1)")
        db.execute("INSERT INTO assistant_events(id,kind,payload,dedupe_key,target,created_at) "
                   "VALUES ('e2','changed','{}','other:1','all',1)")

    result = br.forget_all(store)

    assert sorted(i["title"] for i in result["removed_items"]) == ["Pack gym bag", "Tentative: Gym"]
    assert result["kept_items"] == 0 and result["nudge_events_deleted"] == 1
    assert [r["title"] for r in store._db.execute("SELECT title FROM commitments")] == ["My own reminder"]
    assert store.get(mine["id"]) is not None
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
    assert client.put("/behavior/settings", json={"enabled": True}).status_code == 200
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
        resp = client.put("/behavior/settings", json={"enabled": value})
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


def test_switch_off_right_before_the_write_stops_that_write(tmp_path, store, monkeypatch, writes_allowed):
    rt, _ = make_runtime(tmp_path)
    hub = FakeHub()
    real = rt.policy_block

    def flip(action):
        result = real(action)
        monkeypatch.setenv("WISP_BEHAVIOR", "0")
        return result

    monkeypatch.setattr(rt, "policy_block", flip)
    assert asyncio.run(rt.deliver(store, hub, gym_conflict_nudge(), NOW)) is None
    assert hub.events == [] and store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0


def test_switch_off_right_before_publish_publishes_nothing(tmp_path, store, monkeypatch, writes_allowed):
    rt, _ = make_runtime(tmp_path)
    hub = FakeHub()
    offer_only = Nudge(id="n3", kind="day_preview", title="T", body="B", because="C", created_at=NOW,
                       expires_at=at(23, 0, 1),
                       actions=(Action("open_prompt", {"prompt": "hi"}, auto=False),))
    monkeypatch.setattr(rt, "remember_nudge", lambda *a, **k: None)
    real_live = rt.live
    calls = {"n": 0}

    def live():
        calls["n"] += 1
        return real_live() if calls["n"] < 2 else False       # 1st check passes, the pre-publish one fails

    monkeypatch.setattr(rt, "live", live)
    assert asyncio.run(rt.deliver(store, hub, offer_only, NOW)) is None
    assert hub.events == []


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


def test_forget_waits_for_a_delivery_in_flight(tmp_path, store, writes_allowed):
    """stop() waits for a delivery already past its checks, so its write and publish land
    (and are then erased) before the files go, and none can land afterwards."""
    rt, _ = make_runtime(tmp_path)
    br.set_runtime(rt)
    release = threading.Event()
    entered = threading.Event()

    class SlowHub(FakeHub):
        async def publish(self, event, **kw):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await super().publish(event, **kw)

    result = {}
    loop_thread = threading.Thread(
        target=lambda: result.update(row=asyncio.run(rt.deliver(store, SlowHub(), gym_conflict_nudge(), NOW))),
        daemon=True)
    loop_thread.start()
    assert entered.wait(5)
    t, out = _forget_in_thread(store)
    assert _wait_for(lambda: rt._stopped)
    time.sleep(0.3)
    assert t.is_alive(), "forget must wait for the delivery in flight"
    release.set()
    loop_thread.join(10)
    t.join(10)
    assert not t.is_alive() and result["row"] is not None
    assert list(rt.home.iterdir()) == []
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0     # auto items removed too


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
