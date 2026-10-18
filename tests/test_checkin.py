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


def _reminders(store, *items, undated=()):
    """One native Reminders read: dated items as commitments, undated as a snapshot."""
    from service.assistant.store import parse_undated_snapshot
    snapshot = parse_undated_snapshot(list(undated), len(undated)) if undated else None
    store.sync_source("reminders", [{"source_id": sid, "kind": "reminder", "title": title,
                                     "context": "Reminders", "when_ts": when} for sid, title, when in items],
                      diagnostics={"authorized": True, "count": len(items), "snapshot_started_at": 1000.0},
                      undated=snapshot)


def test_carry_forward_leaves_behind_a_task_from_a_two_week_old_reminder(store):
    """The user's case (build 1998): "Get doordash order" is a Reminders item due
    Oct 2. On Oct 18 the Day tab's Plan time made it a fresh undated Wisp task,
    so the next morning the carry-forward used to move it onto Oct 19 as if it
    were new. It is sixteen days old and must not ride along."""
    due = 1790990100.0                                           # 2026-10-02, the reminder's due time
    _reminders(store, ("E4EFB131-DF70-4000-9778-CC70E0C425B4", "Get doordash order", due))
    stale = task(store, "Get doordash order", "2026-10-18", priority=1, duration_minutes=25)
    fresh = task(store, "Call the bank", "2026-10-18")
    now = at("2026-10-19", 8)
    moved = C.carry_forward(store, "2026-10-19", ZONE, now=now)
    assert [t["title"] for t in moved] == ["Call the bank"]
    assert moved[0]["origin_ts"] == at("2026-10-18", 0) and moved[0]["carried_from"] == "2026-10-18"
    left = store.today_task(stale["id"])
    assert left["day"] == "2026-10-18" and left["status"] == "active", "left where it was, not deleted"
    assert fresh["id"] == moved[0]["id"]
    aged = C.aged_out_today("2026-10-19")
    # The reminder is a week past due and unkept, so the stale policy hides it: the
    # task is left behind for that reason, aged from the reminder's due time.
    assert [(a["title"], a["reason"], a["age_days"]) for a in aged] == [("Get doordash order", "stale_reminder", 16)]
    assert [t["title"] for t in store.today_snapshot("2026-10-19", ZONE)["tasks"]] == ["Call the bank"]
    assert C.carry_forward(store, "2026-10-19", ZONE, now=now) == [], "idempotent"
    assert len(C.aged_out_today("2026-10-19")) == 1


def test_plan_time_task_keeps_the_reminder_due_time_as_its_origin(store, monkeypatch):
    """The Day tab's Plan time on the overdue "Get doordash order" posts origin_ts =
    the reminder's due time (NotchDayView.planTime, DayTimeline.planOrigin). Even
    when no same-titled reminder is left to match (renamed, or planned as
    "Review: …"), the task is sixteen days old on day one and is not carried."""
    from service.assistant import today_api
    monkeypatch.setattr(today_api, "assistant_store", store)
    app = FastAPI()
    app.include_router(today_api.router)
    api = TestClient(app)
    due = 1790990100.0                                           # 2026-10-02
    body = {"title": "Get doordash order", "day": "2026-10-18", "timezone": ZONE, "kind": "task",
            "duration_minutes": 25, "priority": 1}
    made = api.post("/assistant/today/tasks", json=body | {"origin_ts": due})
    assert made.status_code == 201 and made.json()["origin_ts"] == due
    plain = api.post("/assistant/today/tasks", json=body | {"title": "Call the bank"})
    assert plain.status_code == 201 and plain.json().get("origin_ts") is None
    assert api.post("/assistant/today/tasks", json=body | {"origin_ts": -5.0}).status_code >= 400
    assert api.post("/assistant/today/tasks", json=body | {"origin_ts": "yesterday"}).status_code == 422
    # origin_ts is the one new field: anything else unknown is still refused.
    for extra in ({"source_id": "E4EFB131"}, {"carried_from": "2026-10-02"}, {"revision": 7}, {"origin": due}):
        assert api.post("/assistant/today/tasks", json=body | {"origin_ts": due} | extra).status_code == 422, extra
    moved = C.carry_forward(store, "2026-10-19", ZONE, now=at("2026-10-19", 8))
    assert [t["title"] for t in moved] == ["Call the bank"]
    assert [(a["title"], a["age_days"]) for a in C.aged_out_today("2026-10-19")] == [("Get doordash order", 16)]


def test_carry_forward_stops_after_a_week_even_when_the_task_kept_moving(store):
    """"Work on Zybook" was planned on Sept 26 and carried every single morning
    until the user dropped it on Oct 17. The age counts from the first day it
    was planned, not from the last day it was moved to."""
    zybook = task(store, "Work on Zybook", "2026-10-16", carried_from="2026-09-26")
    recent = task(store, "Read the paper", "2026-10-12")
    moved = C.carry_forward(store, "2026-10-17", ZONE, now=at("2026-10-17", 8))
    assert [t["title"] for t in moved] == ["Read the paper"], "five days old still carries; three weeks does not"
    assert moved[0]["origin_ts"] == at("2026-10-12", 0)
    assert store.today_task(zybook["id"])["day"] == "2026-10-16"
    assert [a["title"] for a in C.aged_out_today("2026-10-17")] == ["Work on Zybook"]
    # Three days later the paper is eight days old by origin, although its `day` is Oct 17.
    assert C.carry_forward(store, "2026-10-20", ZONE, now=at("2026-10-20", 8)) == []
    # Zybook is still sitting on Oct 16, so it is noted again; nothing is ever deleted for the user.
    assert sorted(a["title"] for a in C.aged_out_today("2026-10-20")) == ["Read the paper", "Work on Zybook"]
    # The evening tap "Tomorrow" is explicit and still honoured, and keeps the origin.
    saved = C.decide_task(store, moved[0]["id"], "tomorrow", day="2026-10-20", timezone=ZONE, now=at("2026-10-20", 18))
    assert saved["day"] == "2026-10-21" and saved["origin_ts"] == at("2026-10-12", 0)


def test_carry_forward_skips_titles_the_stale_reminder_policy_excludes(store):
    now = at("2026-10-19", 8)
    _reminders(store, undated=[
        {"source_id": "u-ask", "title": "Clean the garage", "context": "Reminders", "created_ts": now - 8 * 86400},
        {"source_id": "u-kept", "title": "Renew passport", "context": "Reminders", "created_ts": now - 20 * 86400},
        {"source_id": "u-fresh", "title": "Buy stamps", "context": "Reminders", "created_ts": now - 2 * 86400},
    ])
    store.keep_stale_reminder("u-kept", "Renew passport")
    task(store, "Clean the garage", "2026-10-18")
    task(store, "Renew passport", "2026-10-18")
    task(store, "buy  stamps", "2026-10-18")                    # title match ignores case and spacing
    moved = C.carry_forward(store, "2026-10-19", ZONE, now=now)
    assert sorted(t["title"] for t in moved) == ["Renew passport", "buy  stamps"]
    assert moved[1]["origin_ts"] == now - 2 * 86400 or moved[0]["origin_ts"] == now - 2 * 86400, \
        "a fresh reminder's creation time becomes the task's origin"
    assert [(a["title"], a["reason"]) for a in C.aged_out_today("2026-10-19")] == [("Clean the garage", "stale_reminder")]


def test_carry_forward_honours_the_store_hidden_reminder_set(store):
    """Once the store knows which reminders are hidden (undated or overdue
    dated, #286), carry-forward asks it rather than re-deriving the rule."""
    class Store(type(store)):
        def hidden_reminder_titles(self, now=None):
            return {"pay the parking ticket"}
    store.__class__ = Store
    task(store, "Pay the  Parking ticket", "2026-10-18")
    task(store, "Water the plants", "2026-10-18")
    moved = C.carry_forward(store, "2026-10-19", ZONE, now=at("2026-10-19", 8))
    assert [t["title"] for t in moved] == ["Water the plants"]
    assert [(a["title"], a["reason"]) for a in C.aged_out_today("2026-10-19")] == [("Pay the  Parking ticket", "stale_reminder")]


def test_origin_ts_is_validated():
    from service.assistant.today import validate_task
    base = {"title": "x", "day": "2026-10-18", "timezone": ZONE, "kind": "task", "duration_minutes": 30,
            "priority": 2, "status": "active", "id": "a", "revision": 1}
    assert validate_task({**base, "origin_ts": None})["origin_ts"] is None
    assert validate_task({**base, "origin_ts": 1790990100.0})["origin_ts"] == 1790990100.0
    for bad in ("soon", True, -1, 0):
        with pytest.raises(ValueError):
            validate_task({**base, "origin_ts": bad})


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
