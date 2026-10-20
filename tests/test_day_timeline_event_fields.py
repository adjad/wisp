"""The Day timeline's Calendar blocks carry what its ⋯ menu's Cancel and Move need.

GET /assistant/today (service/assistant/today_api.py) returns the planner's blocks; a
Calendar block now also says which synced occurrence it is (source_id, all_day) and the
same per-occurrence facts the evening check-in's rows carry (invitee_count,
organizer_is_user, calendar_writable, cancel_blocked, move_blocked), from one helper
(service/assistant/checkin.py occurrence_fields), so the timeline and the card follow
one rule. The planner's own output and the debug replay stay as built.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone as tz

import pytest

from service.assistant import checkin, today_api
from service.assistant.store import AssistantStore
from service.assistant.today import build_plan

HOUR = 3600.0


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(today_api, "assistant_store", s)
    yield s
    s._db.close()


def _event(sid, title, start, length_h=1.0, **extra):
    return {"source_id": sid, "kind": "meeting", "title": title, "when_ts": start,
            "end_ts": start + length_h * HOUR, "context": "Home", "invitee_count": 0, **extra}


def _day_of(ts):
    return datetime.fromtimestamp(ts, tz.utc).strftime("%Y-%m-%d")


def _start():
    # Tomorrow 10:00 UTC: inside the planner's day, never already over.
    return (int(time.time() // 86400) + 1) * 86400 + 10 * HOUR


def test_occurrence_fields_are_the_card_rows_fields(store):
    """One helper for both surfaces: the card's rows and the timeline's blocks say the same."""
    start = _start()
    store.sync_source("calendar", [
        _event("ek-mine", "Design review", start, invitee_count=2, organizer_is_user=True, calendar_writable=True),
        _event("ek-ro", "STAT 5 lecture", start + HOUR, organizer_is_user=True, calendar_writable=False),
        _event("ek-theirs", "1:1 with Sam", start + 2 * HOUR, invitee_count=1, organizer_is_user=False,
               calendar_writable=True),
    ])
    rows = {r["source_id"]: r for r in checkin._event_rows(store, _day_of(start), "UTC")}
    keys = ("invitee_count", "organizer_is_user", "calendar_writable", "cancel_blocked", "move_blocked")
    for sid, when in (("ek-mine", start), ("ek-ro", start + HOUR), ("ek-theirs", start + 2 * HOUR)):
        fields = checkin.occurrence_fields(store, sid, when)
        assert set(fields) == set(keys)
        assert {k: rows[sid][k] for k in keys} == fields
    assert checkin.occurrence_fields(store, "ek-mine", start)["cancel_blocked"] == ""
    assert checkin.occurrence_fields(store, "ek-ro", start + HOUR)["cancel_blocked"] == checkin.READ_ONLY_CALENDAR
    assert "Only the organizer" in checkin.occurrence_fields(store, "ek-theirs", start + 2 * HOUR)["move_blocked"]
    # Not synced (no such occurrence, or no identity to look up): blocked, never offered.
    missing = checkin.occurrence_fields(store, "ek-gone", start)
    assert missing["invitee_count"] is None and missing["cancel_blocked"] == "Not in your synced Calendar."
    assert checkin.occurrence_fields(store, None, start)["move_blocked"] == "Not in your synced Calendar."


def test_calendar_blocks_are_annotated_and_the_planner_output_is_not(store):
    start = _start()
    store.sync_source("calendar", [
        _event("ek-mine", "Design review", start, invitee_count=2, organizer_is_user=True, calendar_writable=True),
        _event("ek-allday", "Reading day", start - 10 * HOUR, 24, all_day=True, calendar_writable=True),
    ])
    day = _day_of(start)
    state = store.today_snapshot(day, "UTC")
    plan = build_plan(day, "UTC", state["tasks"], state["commitments"], state["preferences"], state["sources"],
                      now=start - 5 * HOUR)
    blocks = checkin.annotate_calendar_blocks(store, plan["blocks"], state["commitments"])
    all_day = checkin.annotate_calendar_blocks(store, plan["all_day"], state["commitments"])
    mine = next(b for b in blocks if b["title"] == "Design review")
    assert mine["kind"] == "calendar" and mine["source_id"] == "ek-mine" and mine["all_day"] is False
    assert (mine["invitee_count"], mine["organizer_is_user"], mine["calendar_writable"]) == (2, True, True)
    assert mine["cancel_blocked"] == "" and mine["move_blocked"] == ""
    reading = next(b for b in all_day if b["title"] == "Reading day")
    assert reading["source_id"] == "ek-allday" and reading["all_day"] is True and reading["cancel_blocked"] == ""
    # The planner's own dicts were copied, not changed: a replay compares them as built.
    assert "source_id" not in plan["blocks"][0] and "cancel_blocked" not in plan["all_day"][0]
    # Other kinds pass through untouched, as the same objects.
    task_block = {"id": "task:t1", "title": "Lab", "start": start, "end": start + HOUR, "kind": "task",
                  "task_id": "t1", "warnings": []}
    out = checkin.annotate_calendar_blocks(store, [task_block], state["commitments"])
    assert out[0] is task_block
    # A Calendar block whose commitment is no longer in the snapshot is left alone too.
    orphan = {"id": "c-gone", "title": "Gone", "start": start, "end": start + HOUR, "kind": "calendar",
              "task_id": None, "warnings": []}
    assert checkin.annotate_calendar_blocks(store, [orphan], state["commitments"])[0] is orphan


def test_today_snapshot_serves_the_fields_on_saved_and_proposed_plans(store):
    """GET /assistant/today: the saved plan's blocks and, when the workspace builds one,
    the proposed plan's blocks carry the fields; the debug replay's expected_plan does not."""
    start = _start()
    store.sync_source("calendar", [
        _event("ek-ro", "STAT 5 lecture", start, organizer_is_user=True, calendar_writable=False),
        _event("ek-mine", "Design review", start + 2 * HOUR, invitee_count=3, organizer_is_user=True,
               calendar_writable=True),
    ])
    result = today_api.snapshot(_day_of(start), "UTC", debug=True)
    by_title = {b["title"]: b for b in result["blocks"]}
    assert by_title["STAT 5 lecture"]["calendar_writable"] is False
    assert by_title["STAT 5 lecture"]["cancel_blocked"] == checkin.READ_ONLY_CALENDAR
    assert by_title["STAT 5 lecture"]["move_blocked"] == checkin.READ_ONLY_CALENDAR
    assert by_title["Design review"]["source_id"] == "ek-mine"
    assert by_title["Design review"]["invitee_count"] == 3
    assert by_title["Design review"]["cancel_blocked"] == "" and by_title["Design review"]["move_blocked"] == ""
    assert all("source_id" not in b for b in result["replay"]["expected_plan"]["blocks"])
    proposed = result.get("workspace", {}).get("proposed_plan")
    if proposed:   # the workspace is optional (private settings, sources); when it is there, it matches
        proposed_titles = {b["title"]: b for b in proposed["blocks"] if b["kind"] == "calendar"}
        assert proposed_titles["Design review"]["invitee_count"] == 3
        assert proposed_titles["STAT 5 lecture"]["cancel_blocked"] == checkin.READ_ONLY_CALENDAR
