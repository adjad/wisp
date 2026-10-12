"""/behavior/* endpoints on a minimal app (no service.main import). Synthetic only."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import behavior_api
from service.assistant import behavior_runtime as br
from service.behavior.types import Action, Nudge
from tests.test_behavior_runtime import NOW, FakeEngine, FakeHub, at, gym_conflict_nudge, make_runtime


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("WISP_BEHAVIOR", "WISP_FOUNDER_DEMO", "WISP_BEHAVIOR_SYNTHETIC_ONLY", "WISP_QA_SEED"):
        monkeypatch.delenv(key, raising=False)
    br.set_runtime(None)
    yield
    br.set_runtime(None)


@pytest.fixture
def store(tmp_path):
    from service.assistant.store import AssistantStore
    return AssistantStore(tmp_path / "assistant.db")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(behavior_api.router)
    return TestClient(app)


@pytest.fixture
def live(monkeypatch, tmp_path, store):
    """Behavior on, a fake engine with one delivered nudge plus one offering a reversible action."""
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    offered = Nudge(id="n2", kind="day_preview", title="Tomorrow", body="Busy morning.",
                    because="3 meetings before noon", created_at=NOW, expires_at=at(23, 0, 1),
                    actions=(Action("add_reminder", {"title": "Prep for board", "when_ts": at(20, 0)},
                                    auto=False, label="Remind me tonight"),))
    engine = FakeEngine(nudges=[gym_conflict_nudge(), offered])
    rt, _ = make_runtime(tmp_path, engine)
    br.set_runtime(rt)
    hub = FakeHub()
    monkeypatch.setattr(behavior_api, "_store_hub", lambda: (store, hub))
    return rt, engine, hub


def test_disabled_status_and_409(client):
    assert client.get("/behavior/status").json()["enabled"] is False
    for method, path, body in [("post", "/behavior/run", None), ("post", "/behavior/reset", None),
                               ("post", "/behavior/clear", None), ("get", "/behavior/nudges", None),
                               ("get", "/behavior/patterns", None),
                               ("get", "/behavior/predict", None), ("get", "/behavior/profile", None),
                               ("post", "/behavior/patterns/p1", {"status": "rejected"}),
                               ("post", "/behavior/nudges/n1/feedback", {"verdict": "useful"}),
                               ("post", "/behavior/nudges/n1/undo", None),
                               ("post", "/behavior/nudges/n1/act", {"action_index": 0})]:
        resp = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert resp.status_code == 409, path
        assert "WISP_BEHAVIOR=1" in resp.json()["detail"]


def test_run_publishes_and_status(client, live, store):
    rt, engine, hub = live
    data = client.post("/behavior/run").json()
    assert data["ok"] and data["published"] == 2 and set(data["nudge_ids"]) == {"n1", "n2"}
    status = client.get("/behavior/status").json()
    assert status["enabled"] and status["synthetic"] and status["active_engine"] == "local"
    assert status["delivered_nudges"] == 2 and status["auto_actions"] == 2
    assert status["engine"]["patterns"] == 3


def test_patterns_list_filter_and_status(client, live):
    pats = client.get("/behavior/patterns").json()["patterns"]
    assert {p["id"] for p in pats} == {"p_gym", "p_sam", "p_b2b"}
    assert pats[0]["window"]["weekdays"] == [0, 1, 2, 3, 4]
    assert client.get("/behavior/patterns", params={"kind": "habit"}).json()["patterns"][0]["id"] == "p_gym"
    assert client.get("/behavior/patterns", params={"kind": "nope"}).status_code == 422
    assert client.get("/behavior/patterns", params={"status": "nope"}).status_code == 422
    ok = client.post("/behavior/patterns/p_gym", json={"status": "rejected"}).json()
    assert ok["ok"] and ok["pattern"]["status"] == "rejected"
    assert client.get("/behavior/patterns", params={"status": "rejected"}).json()["patterns"][0]["id"] == "p_gym"
    assert client.post("/behavior/patterns/missing", json={"status": "rejected"}).status_code == 404
    assert client.post("/behavior/patterns/p_gym", json={"status": "deleted"}).status_code == 422
    assert client.post("/behavior/patterns/p_gym", json=["rejected"]).status_code == 422


def test_predict_and_profile(client, live, monkeypatch):
    rt, engine, hub = live
    monkeypatch.setattr(rt, "clock", lambda: NOW)
    preds = client.get("/behavior/predict", params={"day": "tomorrow"}).json()
    assert preds["day"] == "tomorrow" and [p["subject"] for p in preds["predictions"]] == ["gym", "Team sync"]
    assert client.get("/behavior/predict", params={"day": "today"}).json()["predictions"] == []
    assert client.get("/behavior/predict", params={"day": "someday"}).status_code == 422
    prof = client.get("/behavior/profile").json()
    assert "Gym weekday mornings" in prof["card"] and len(prof["patterns"]) == 3 and prof["summary"]


def test_feedback(client, live):
    rt, engine, hub = live
    client.post("/behavior/run")
    assert client.post("/behavior/nudges/n1/feedback", json={"verdict": "meh"}).status_code == 422
    assert client.post("/behavior/nudges/zzz/feedback", json={"verdict": "useful"}).status_code == 404
    assert client.post("/behavior/nudges/n1/feedback", json={"verdict": "wrong"}).json()["ok"]
    assert engine.feedbacks[-1].verdict == "wrong"


def test_undo_reverts_auto_actions_and_pings(client, live, store):
    rt, engine, hub = live
    client.post("/behavior/run")
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2
    data = client.post("/behavior/nudges/n1/undo").json()
    assert data["ok"] and len(data["removed"]) == 2
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0
    assert engine.feedbacks[-1].verdict == "undo"
    assert hub.events[-1]["event"] == {"type": "changed"}
    assert client.post("/behavior/nudges/unknown/undo").status_code == 404
    listed = {n["id"]: n for n in client.get("/behavior/nudges").json()["nudges"]}
    assert set(listed) == {"n1", "n2"} and listed["n1"]["verdict"] == "undo"
    assert listed["n1"]["can_undo"] is False and all(a["undone"] for a in listed["n1"]["auto_performed"])


def test_act_open_prompt_and_offered_reminder(client, live, store):
    rt, engine, hub = live
    client.post("/behavior/run")
    act = client.post("/behavior/nudges/n1/act", json={"action_index": 2}).json()
    assert act == {"ok": True, "type": "open_prompt", "label": "Draft reschedule note",
                   "prompt": "Draft a note asking to move my 7:00 call to 9:00"}
    assert engine.feedbacks[-1].verdict == "useful"
    created = client.post("/behavior/nudges/n2/act", json={"action_index": 0}).json()
    assert created["ok"] and created["created"]["title"] == "Prep for board" and created["can_undo"]
    assert client.post("/behavior/nudges/n2/undo").json()["removed"][0]["title"] == "Prep for board"
    assert client.post("/behavior/nudges/n1/act", json={"action_index": 9}).status_code == 404
    assert client.post("/behavior/nudges/n1/act", json={"action_index": "0"}).status_code == 422
    assert client.post("/behavior/nudges/n1/act", json={"action_index": -1}).status_code == 422
    assert client.post("/behavior/nudges/zzz/act", json={"action_index": 0}).status_code == 404


def test_reset_synthetic_only(client, live, store, monkeypatch, tmp_path):
    rt, engine, hub = live
    client.post("/behavior/run")
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2
    out = client.post("/behavior/clear").json()
    assert out["ok"] and out["commitments"] == 2 and out["events"] == 0
    assert store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0
    real, _ = make_runtime(tmp_path / "real", FakeEngine(), synthetic=False)
    br.set_runtime(real)
    assert client.post("/behavior/reset").status_code == 409


def test_run_surfaces_busy_when_another_tick_holds_the_lock(client, live, monkeypatch):
    rt, engine, hub = live

    async def busy(*a, **k):
        raise br.TickBusy("another behavior run is still in progress after 30s")
    monkeypatch.setattr(rt, "tick", busy)
    data = client.post("/behavior/run").json()
    assert data["ok"] is False and data["busy"] is True and data["published"] == 0
    assert "in progress" in data["detail"]


@pytest.mark.parametrize("path,body", [
    ("/behavior/patterns/p1", {"status": "rejected"}),
    ("/behavior/nudges/n1/feedback", {"verdict": "useful"}),
    ("/behavior/nudges/n1/act", {"action_index": 0}),
    ("/behavior/nudges/n1/undo", None),
])
def test_state_changing_routes_403_in_read_only_remote(client, live, monkeypatch, path, body):
    monkeypatch.setattr(behavior_api, "remote_read_only_mode", lambda: True)
    resp = client.post(path, json=body) if body is not None else client.post(path)
    assert resp.status_code == 403
    assert client.get("/behavior/patterns").status_code == 200
