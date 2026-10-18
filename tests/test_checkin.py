"""Evening check-in, morning carry-forward and the weekly goal pulse
(service/assistant/checkin.py, checkin_api.py). Disposable store and state files only."""
from __future__ import annotations

from datetime import datetime
import time
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.assistant import checkin as C
from service.assistant.store import AssistantStore
from service.memory import profile as P

ZONE = "America/Los_Angeles"


def at(day: str, hour: int, minute: int = 0) -> float:
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


@pytest.fixture(autouse=True)
def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_PATH", tmp_path / "checkins.json")
    monkeypatch.setattr(P, "PROFILE_PATH", tmp_path / "profile.json")


@pytest.fixture
def store(tmp_path):
    return AssistantStore(tmp_path / "assistant.db")


def task(store, title, day, **extra):
    return store.today_save_task({"title": title, "day": day, "timezone": ZONE, "kind": "task",
                                  "duration_minutes": 30, **extra})


# --- due / build ------------------------------------------------------------------

def test_due_only_in_the_evening_of_that_day(store):
    day = "2026-10-17"
    assert C.is_due(at(day, 16, 59), ZONE) is False
    assert C.is_due(at(day, 17), ZONE) is True
    card = C.build(store, day, ZONE, now=at(day, 18))
    assert card["due"] is True and card["completed"] is False and card["tasks"] == []
    assert C.build(store, day, ZONE, now=at("2026-10-18", 18))["due"] is False, "yesterday's card is not due"


def test_build_lists_active_tasks_and_counts_done(store):
    day = "2026-10-17"
    a = task(store, "Write intro", day)
    b = task(store, "Call bank", day)
    store.today_save_task({"status": "done"}, task_id=b["id"], revision=b["revision"])
    task(store, "Later thing", "2026-10-18")
    card = C.build(store, day, ZONE, now=at(day, 19))
    assert card["planned"] == 2 and card["done"] == 1
    assert [t["title"] for t in card["tasks"]] == ["Write intro"]
    assert card["tasks"][0]["revision"] == a["revision"]
    assert card["tomorrow"]["day"] == "2026-10-18"


# --- decisions -------------------------------------------------------------------------

def test_done_tomorrow_and_drop(store):
    day, tomorrow = "2026-10-17", "2026-10-18"
    now = at(day, 19)
    a = task(store, "Write intro", day)
    b = task(store, "Call bank", day, due_ts=at(day, 14), pinned_start=at(day, 13))
    c = task(store, "Gym", day)
    assert C.decide_task(store, a["id"], "done", day=day, timezone=ZONE, now=now)["status"] == "done"
    moved = C.decide_task(store, b["id"], "tomorrow", day=day, timezone=ZONE, now=now)
    assert moved["day"] == tomorrow and moved["carried_from"] == day
    assert moved["pinned_start"] is None and moved["due_ts"] is None, "a missed deadline and a pin do not follow"
    dropped = C.decide_task(store, c["id"], "drop", day=day, timezone=ZONE, now=now)
    assert dropped["status"] == "dismissed"
    card = C.build(store, day, ZONE, now=now)
    assert card["tasks"] == [] and card["done"] == 1
    assert [t["title"] for t in store.today_snapshot(tomorrow, ZONE)["tasks"]] == ["Call bank"]
    decisions = C.day_record(day)["decisions"]
    assert [d["decision"] for d in decisions] == ["done", "tomorrow", "drop"]
    with pytest.raises(ValueError):
        C.decide_task(store, a["id"], "maybe", day=day, timezone=ZONE, now=now)
    with pytest.raises(KeyError):
        C.decide_task(store, "nope", "done", day=day, timezone=ZONE, now=now)


def test_tomorrow_keeps_a_future_deadline_and_the_original_origin(store):
    day = "2026-10-17"
    t = task(store, "Essay", day, due_ts=at("2026-10-20", 23), carried_from="2026-10-15")
    moved = C.decide_task(store, t["id"], "tomorrow", day=day, timezone=ZONE, now=at(day, 19))
    assert moved["due_ts"] == at("2026-10-20", 23)
    assert moved["carried_from"] == "2026-10-15", "the first planned day is what carries"


def test_complete_and_times_moved(store):
    C.complete("2026-10-17", now=1.0)
    assert C.day_record("2026-10-17")["completed_at"] == 1.0
    for day in ("2026-10-14", "2026-10-15", "2026-10-16"):
        t = task(store, "Gym", day)
        C.decide_task(store, t["id"], "tomorrow", day=day, timezone=ZONE, now=at(day, 19))
    assert C.times_moved("gym", before_day="2026-10-17") == 3
    assert C.times_moved("Gym", before_day="2026-10-15") == 1
    assert C.times_moved("Essay", before_day="2026-10-17") == 0


# --- carry-forward -------------------------------------------------------------------

def test_carry_forward_moves_only_active_earlier_tasks_once(store):
    today = "2026-10-17"
    old = task(store, "Old thing", "2026-10-15", pinned_start=at("2026-10-15", 10), due_ts=at("2026-10-15", 12))
    done = task(store, "Finished", "2026-10-16")
    store.today_save_task({"status": "done"}, task_id=done["id"], revision=done["revision"])
    task(store, "Today's own", today)
    task(store, "Future", "2026-10-19")
    now = at(today, 8)
    moved = C.carry_forward(store, today, ZONE, now=now)
    assert [t["title"] for t in moved] == ["Old thing"]
    assert moved[0]["day"] == today and moved[0]["carried_from"] == "2026-10-15"
    assert moved[0]["pinned_start"] is None and moved[0]["due_ts"] is None
    assert C.carry_forward(store, today, ZONE, now=now) == [], "idempotent"
    assert [c["title"] for c in C.carried_today(today)] == ["Old thing"]
    titles = sorted(t["title"] for t in store.today_snapshot(today, ZONE)["tasks"])
    assert titles == ["Old thing", "Today's own"]
    assert old["id"] == moved[0]["id"]


def test_workspace_explains_a_carried_task(store):
    from service.assistant import daily_workspace as D
    today = "2026-10-17"
    task(store, "Old thing", "2026-10-16")
    now = at(today, 8)
    C.carry_forward(store, today, ZONE, now=now)
    state = store.today_snapshot(today, ZONE)
    out = D.assess(today, ZONE, state, {}, now=now)
    item = next(i for i in out["attention"] if i["title"] == "Old thing")
    assert item["reason"].startswith("Carried over from 2026-10-16.")


# --- goal pulse ---------------------------------------------------------------------

def test_goal_pulse_is_weekly_and_prefers_behind():
    now = 10_000_000.0
    assert C.goal_pulse(None, now=now) is None
    assert C.goal_pulse(P.empty(), now=now) is None
    P.update({"goals": ["Ship it", "Run far"]}, now=now)
    assert C.goal_pulse(P.load(), now=now + 86400) is None, "not a week yet"
    pulse = C.goal_pulse(P.load(), now=now + 8 * 86400)
    assert pulse and pulse["goal"] in ("Ship it", "Run far") and pulse["options"] == ["on_track", "behind", "done"]
    P.set_goal_status("Run far", "behind", now=now + 8 * 86400)
    assert C.goal_pulse(P.load(), now=now + 9 * 86400) is None, "answered this week"
    later = C.goal_pulse(P.load(), now=now + 16 * 86400)
    assert later["goal"] == "Run far" and later["status"] == "behind"
    P.set_goal_status("Run far", "done", now=now + 16 * 86400)
    P.set_goal_status("Ship it", "done", now=now + 16 * 86400)
    assert C.goal_pulse(P.load(), now=now + 40 * 86400) is None, "every goal done"
    with pytest.raises(ValueError):
        P.set_goal_status("Ship it", "meh")
    with pytest.raises(ValueError):
        P.set_goal_status("Not a goal", "done")


def test_goal_status_shapes_the_brief_focus():
    now = time.mktime((2026, 10, 17, 8, 0, 0, 0, 0, -1))
    P.update({"goals": ["A", "B", "C"]}, now=now)
    P.set_goal_status("C", "behind", now=now)
    profile = P.load()
    assert P.goal_in_focus(profile, now) == "C"
    assert "you said you're behind" in P.keeping_in_mind(profile, now)
    P.set_goal_status("C", "done", now=now)
    assert P.goal_in_focus(P.load(), now) in ("A", "B")
    # Editing goals drops statuses for goals that are gone.
    P.update({"goals": ["A"]}, now=now)
    assert P.load()["goal_status"] == {}


def test_keeping_in_mind_names_carried_tasks_even_without_a_profile():
    text = P.keeping_in_mind(P.empty(), 0.0, carried_over=[{"title": "Old thing"}, {"title": "Essay"}])
    assert "2 tasks carried over from before: Old thing; Essay. Already on today's plan." in text
    assert P.keeping_in_mind(P.empty(), 0.0, carried_over=[]) == ""


# --- API -----------------------------------------------------------------------------

@pytest.fixture
def client(store, monkeypatch):
    from service.assistant import checkin_api
    monkeypatch.setattr(checkin_api, "assistant_store", store)
    app = FastAPI()
    app.include_router(checkin_api.router)
    return TestClient(app)


def test_api_round_trip(client, store, monkeypatch):
    day = "2026-10-17"
    monkeypatch.setattr(time, "time", lambda: at(day, 19))
    t = task(store, "Write intro", day)
    task(store, "Old thing", "2026-10-16")
    card = client.get("/assistant/checkin", params={"day": day, "timezone": ZONE}).json()
    assert card["due"] is True and sorted(x["title"] for x in card["tasks"]) == ["Old thing", "Write intro"]
    assert [c["title"] for c in card["carried"]] == ["Old thing"], "opening the card carried forward"
    after = client.post(f"/assistant/checkin/tasks/{t['id']}",
                        json={"decision": "done", "day": day, "timezone": ZONE}).json()
    assert after["done"] == 1 and [x["title"] for x in after["tasks"]] == ["Old thing"]
    assert client.post(f"/assistant/checkin/tasks/{t['id']}",
                       json={"decision": "nope", "day": day, "timezone": ZONE}).status_code == 422
    assert client.post("/assistant/checkin/tasks/missing",
                       json={"decision": "done", "day": day, "timezone": ZONE}).status_code == 404
    done = client.post("/assistant/checkin/complete", json={"day": day, "timezone": ZONE}).json()
    assert done["completed"] is True
    P.update({"goals": ["Ship it"]}, now=at(day, 19) - 10 * 86400)
    pulse = client.get("/assistant/checkin", params={"day": day, "timezone": ZONE}).json()["goal_pulse"]
    assert pulse["goal"] == "Ship it"
    out = client.post("/assistant/checkin/goal", json={"goal": "Ship it", "status": "behind"}).json()
    assert out["goal_status"]["Ship it"]["status"] == "behind" and out["goal_pulse"] is None
    assert client.post("/assistant/checkin/goal", json={"goal": "Ship it", "status": "meh"}).status_code == 422
