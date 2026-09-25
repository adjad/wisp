"""Synthetic Today contracts; never sync or mutate real native sources."""
from datetime import datetime
from zoneinfo import ZoneInfo
import math
import os
import tempfile

# Safe even when this test is run directly without the repository runner.
_scratch = tempfile.TemporaryDirectory(prefix="wisp-today-contract-")
os.environ["WISP_HOME"] = _scratch.name

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from service.assistant.store import AssistantStore
from service.assistant import today_api
from service.assistant.today import (RevisionConflict, build_plan, day_bounds,
                                     validate_task, validate_preferences, wall_time)

DAY = "2026-09-24"
ZONE = "America/Los_Angeles"
NOW = datetime(2026, 9, 24, 8, tzinfo=ZoneInfo(ZONE)).timestamp()
PREF = dict(start_minute=540, end_minute=1080, not_before=None)


def at(hour, minute=0):
    return datetime(2026, 9, 24, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


def ready(now=NOW):
    return {s: dict(available=True, last_sync=now, diagnostics=dict(coverage_start=NOW-86400*365,
                    coverage_end=NOW+86400*30)) for s in ("calendar", "reminders")}


def task(**values):
    return dict(id="t1", title="Study", day=DAY, kind="study", duration_minutes=60,
                priority=2, status="active", due_ts=None, pinned_start=None, revision=1) | values


def event(id="e1", start=None, end=None, **values):
    return dict(id=id, source_id=id, source="calendar", title="Class", kind="event",
                when_ts=at(9) if start is None else start, end_ts=at(10) if end is None else end,
                all_day=False) | values


def plan(tasks=None, commitments=None, status=None, preferences=None, **kw):
    return build_plan(DAY, ZONE, tasks if tasks is not None else [task()], commitments or [],
                      preferences or PREF, ready() if status is None else status, now=NOW, **kw)


@pytest.fixture
def store(tmp_path):
    s = AssistantStore(tmp_path / "assistant.db")
    yield s
    s._db.close()


def test_places_after_calendar_and_before_deadline():
    p = plan(commitments=[event()], tasks=[task(due_ts=at(11))])
    assert p["blocks"][-1]["start"] == at(10)
    assert p["blocks"][-1]["end"] == at(11)
    assert not p["unscheduled"]


def test_no_overlap_when_same_title_start_have_different_ends(store):
    store.sync_source("calendar", [event("e1"), event("e2", end=at(12))])
    state = store.today_snapshot(DAY, ZONE)
    p = plan(commitments=state["commitments"])
    assert len(state["commitments"]) == 2
    assert p["blocks"][-1]["start"] == at(12)


def test_reminders_are_deadlines_not_busy():
    p = plan(commitments=[event(source="reminders")])
    assert p["blocks"][0]["start"] == at(9)
    assert len(p["deadlines"]) == 1


def test_unscheduled_when_no_contiguous_gap():
    p = plan(commitments=[event(start=at(9, 30), end=at(18))])
    assert not [b for b in p["blocks"] if b["task_id"]]
    assert "uninterrupted" in p["unscheduled"][0]["reason"]


def test_pin_preserved_and_conflict_reported():
    p = plan(tasks=[task(pinned_start=at(9, 30))], commitments=[event()])
    pinned = next(b for b in p["blocks"] if b["task_id"])
    assert pinned["start"] == at(9, 30)
    assert any("Overlaps" in w for w in pinned["warnings"])


def test_priority_and_deadline_order():
    p = plan(tasks=[task(id="normal", priority=2), task(id="later", priority=1, due_ts=at(12)),
                    task(id="first", priority=1, due_ts=at(10))])
    assert [b["task_id"] for b in p["blocks"]] == ["first", "later", "normal"]


def test_done_dismissed_and_other_days_not_scheduled():
    assert not plan(tasks=[task(status="done"), task(status="dismissed"), task(day="2026-09-25")])["blocks"]


@pytest.mark.parametrize("status", [{}, {"calendar": {"available": False, "last_sync": NOW}},
                                    {"calendar": {"available": True, "last_sync": NOW - 181}},
                                    {"calendar": {"available": True, "syncing": True, "last_sync": NOW}}])
def test_no_guessed_free_time_without_fresh_calendar(status):
    p = plan(status=status)
    assert p["provisional"] and len(p["unscheduled"]) == 1 and not p["blocks"]


def test_missing_end_is_visible_and_blocks_flexible():
    p = plan(commitments=[event() | {"end_ts": None}])
    assert p["blocks"][0]["end"] is None
    assert p["provisional"] and p["unscheduled"]


def test_all_day_requires_explicit_time_choice():
    lo, hi = day_bounds(DAY, ZONE)
    p = plan(commitments=[event(start=lo, end=hi, all_day=True)])
    assert len(p["all_day"]) == 1 and not p["blocks"] and p["unscheduled"]


def test_overnight_overlap_and_exclusive_end(store):
    lo, hi = day_bounds(DAY, ZONE)
    store.sync_source("calendar", [event("overnight", start=lo - 7200, end=at(10)),
                                   event("past", start=lo - 7200, end=lo),
                                   event("tomorrow", start=hi, end=hi + 3600)])
    state = store.today_snapshot(DAY, ZONE)
    assert [c["source_id"] for c in state["commitments"]] == ["overnight"]
    assert plan(commitments=state["commitments"])["blocks"][-1]["start"] == at(10)


def test_running_late_and_duration_edits():
    p = plan(preferences=PREF | {"not_before": at(10, 15)}, tasks=[task(duration_minutes=90)])
    assert p["blocks"][0]["start"] == at(10, 15)
    assert p["blocks"][0]["end"] == at(11, 45)


def test_past_time_never_scheduled():
    p = build_plan(DAY, ZONE, [task()], [], PREF, ready(at(10, 5)), now=at(10, 5) + 5)
    assert p["blocks"][0]["start"] == at(10, 6)


def test_end_time_update_recurrence_and_replace_cleanup(store):
    store.sync_source("calendar", [event("same", start=at(9), end=at(10)),
                                   event("same", start=at(11), end=at(12))])
    assert len(store.today_snapshot(DAY, ZONE)["commitments"]) == 2
    store.sync_source("calendar", [event("same", start=at(9), end=at(10, 30))])
    assert store.today_snapshot(DAY, ZONE)["commitments"][0]["end_ts"] == at(10, 30)
    assert store._db.execute("SELECT COUNT(*) FROM calendar_event_ends").fetchone()[0] == 1


def test_invalid_end_rolls_back_entire_sync(store):
    store.sync_source("calendar", [event()])
    with pytest.raises(ValueError):
        store.sync_source("calendar", [event("new"), event("bad") | {"end_ts": float("nan")}])
    assert [c["source_id"] for c in store.today_snapshot(DAY, ZONE)["commitments"]] == ["e1"]


def test_restart_and_source_replace_do_not_delete_local_tasks(store):
    saved = store.today_save_task({k: v for k, v in task().items() if k not in ("id", "revision")})
    store.today_save_preferences(DAY, ZONE, PREF | {"not_before": at(10)}, 0)
    store.sync_source("calendar", [event()])
    store.sync_source("calendar", [])
    second = AssistantStore(store.path)
    try:
        state = second.today_snapshot(DAY, ZONE)
        assert state["tasks"] == [saved] and state["revision"] == 1
        assert state["preferences"]["not_before"] == at(10)
    finally:
        second._db.close()


def test_task_revision_conflict_across_connections(store):
    saved = store.today_save_task(dict(title="Study", day=DAY, duration_minutes=45))
    second = AssistantStore(store.path)
    try:
        store.today_save_task({"duration_minutes": 60}, task_id=saved["id"], revision=1)
        with pytest.raises(RevisionConflict):
            second.today_save_task({"status": "done"}, task_id=saved["id"], revision=1)
        assert second.today_snapshot(DAY, ZONE)["tasks"][0]["duration_minutes"] == 60
    finally:
        second._db.close()


def test_preference_conflict_does_not_overwrite(store):
    store.today_save_preferences(DAY, ZONE, PREF, 0)
    with pytest.raises(RevisionConflict):
        store.today_save_preferences(DAY, ZONE, PREF | {"start_minute": 600}, 0)
    assert store.today_snapshot(DAY, ZONE)["preferences"] == PREF


@pytest.mark.parametrize("day,hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_dst_local_day_bounds(day, hours):
    lo, hi = day_bounds(day, ZONE)
    assert hi - lo == hours * 3600


@pytest.mark.parametrize("day,minute", [("2026-03-08", 150), ("2026-11-01", 90)])
def test_ambiguous_and_missing_working_times_rejected(day, minute):
    with pytest.raises(ValueError, match="daylight-saving"):
        wall_time(day, ZONE, minute)


@pytest.mark.parametrize("values", [dict(duration_minutes=True), dict(duration_minutes=0), dict(title=" "),
                                     dict(priority=4), dict(due_ts=math.inf), dict(pinned_start=True),
                                     dict(day="not-a-day"), dict(kind="event"), dict(status="deleted")])
def test_invalid_tasks(values):
    with pytest.raises(ValueError):
        validate_task(task(**values))


def test_native_end_table_does_not_change_legacy_schema(store):
    columns = {r["name"] for r in store._db.execute("PRAGMA table_info(commitments)")}
    assert "end_ts" not in columns
    assert len(columns) == 16


def test_api_validation_and_conflicts(store, monkeypatch):
    monkeypatch.setattr(today_api, "assistant_store", store)
    monkeypatch.setattr(today_api.time, "time", lambda: NOW)
    app = FastAPI()
    app.include_router(today_api.router)
    with TestClient(app) as client:
        url = "/assistant/today"
        assert client.get(url, params=dict(day=DAY, timezone="bad-zone")).status_code == 422
        assert client.post(url + "/tasks", json=dict(title="x", day=DAY, duration_minutes=True)).status_code == 422
        assert client.post(url + "/tasks", json=dict(title="x", day=DAY, duration_minutes=45, due_ts=True)).status_code == 422
        saved = client.post(url + "/tasks", json=dict(title="Study", day=DAY, duration_minutes=45))
        assert saved.status_code == 201
        ident = saved.json()["id"]
        assert client.patch(url + "/tasks/" + ident, json=dict(revision=1, status="done")).status_code == 200
        assert client.patch(url + "/tasks/" + ident, json=dict(revision=1, status="active")).status_code == 409
        assert client.patch(url + "/tasks/" + ident, json=dict(revision=2, title=None)).status_code == 422
        assert client.patch(url + "/tasks/missing", json=dict(revision=1, status="done")).status_code == 404
        assert client.post(url + "/replan", json=dict(day=DAY, timezone=ZONE, revision=0)).status_code == 200
        assert client.post(url + "/replan", json=dict(day=DAY, timezone=ZONE, revision=0)).status_code == 409
        body = client.get(url, params=dict(day=DAY, timezone=ZONE)).json()
        assert body["tasks"][0]["status"] == "done"


def test_missing_or_outside_coverage_never_asserts_free_time():
    status = ready()
    status["calendar"]["diagnostics"] = {}
    assert plan(status=status)["unscheduled"]
    status["calendar"]["diagnostics"] = dict(coverage_start=NOW, coverage_end=at(12))
    assert plan(status=status)["unscheduled"]


def test_unknown_overnight_duration_cannot_be_treated_as_free(store):
    lo, _ = day_bounds(DAY, ZONE)
    store.sync_source("calendar", [event(start=lo - 3600) | {"end_ts": None}])
    p = plan(commitments=store.today_snapshot(DAY, ZONE)["commitments"])
    assert p["provisional"] and p["blocks"] and not p["unscheduled"]


def test_duplicate_occurrence_keeps_last_end(store):
    store.sync_source("calendar", [event(), event(end=at(12))])
    assert store.today_snapshot(DAY, ZONE)["commitments"][0]["end_ts"] == at(12)


def test_maximum_date_is_validation_error():
    with pytest.raises(ValueError):
        day_bounds("9999-12-31", ZONE)


def test_calendar_sync_endpoint_validates_before_replacement(store, monkeypatch):
    import asyncio
    from service import main
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    monkeypatch.setattr(main, "assistant_store", store)
    monkeypatch.setattr(main.assistant_hub, "publish", AsyncMock())
    monkeypatch.setattr(main.assistant_scheduler, "record_sync", lambda *a, **kw: None)
    asyncio.run(main.assistant_sync_calendar({"events": [event()]}))
    assert store.today_snapshot(DAY, ZONE)["commitments"][0]["end_ts"] == at(10)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.assistant_sync_calendar({"events": [event("new"), event(end=at(8))]}))
    assert error.value.status_code == 422
    assert [c["source_id"] for c in store.today_snapshot(DAY, ZONE)["commitments"]] == ["e1"]


def test_manual_fixed_event_without_duration_withholds_new_slots(store):
    store.add_manual("Meeting", at(9), kind="meeting")
    p = plan(commitments=store.today_snapshot(DAY, ZONE)["commitments"])
    assert len(p["blocks"]) == 1 and p["unscheduled"] and not p["deadlines"]


def test_mirrored_reminder_is_one_deadline(store):
    store.add_manual("Homework", at(12))
    store.sync_source("reminders", [dict(source_id="native", title="Homework", kind="reminder", when_ts=at(12))])
    state = store.today_snapshot(DAY, ZONE)
    assert len(plan(commitments=state["commitments"])["deadlines"]) == 1


def test_moving_pinned_task_to_another_day_requires_unpin(store):
    saved = store.today_save_task(dict(title="Study", day=DAY, timezone=ZONE, duration_minutes=45, pinned_start=at(9)))
    with pytest.raises(ValueError, match="unpin"):
        store.today_save_task(dict(day="2026-09-25"), task_id=saved["id"], revision=1)
    changed = store.today_save_task(dict(day="2026-09-25", pinned_start=None), task_id=saved["id"], revision=1)
    assert changed["day"] == "2026-09-25" and changed["revision"] == 2


def test_pin_day_uses_task_timezone():
    before = datetime(2026, 9, 24, 1, tzinfo=ZoneInfo("UTC")).timestamp()
    with pytest.raises(ValueError, match="Pinned start"):
        validate_task(task(timezone=ZONE, pinned_start=before))


def test_snapshot_uses_read_transaction(store):
    statements = []
    store._db.set_trace_callback(statements.append)
    store.today_snapshot(DAY, ZONE)
    assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"


def test_native_success_receipt_preserves_event_duration(store):
    import json
    payload = dict(type="create_calendar_event", action_id="fixture-action", title="Meeting",
                   when_ts=at(10), duration_min=30, location="")
    store._db.execute("INSERT INTO assistant_events(id,kind,payload,dedupe_key,target,created_at,state,claim_token) "
                      "VALUES (?,?,?,?,?,?,'pending','fixture-claim')",
                      ("fixture-event", "create_calendar_event", json.dumps(payload), "action:fixture-action",
                       json.dumps({"type": "calendar"}), NOW))
    store._db.commit()
    store.complete_calendar_action("fixture-event", "create_calendar_event", "fixture-claim",
                                   dict(ok=True, status="succeeded", source_id="native-id", error=""))
    assert store.today_snapshot(DAY, ZONE)["commitments"][0]["end_ts"] == at(10, 30)


def test_pinned_outside_hours_is_retained_with_warning():
    p = plan(tasks=[task(pinned_start=at(7), due_ts=at(7, 30))])
    assert p["blocks"][0]["start"] == at(7)
    assert len(p["blocks"][0]["warnings"]) == 3


def test_stale_reminders_only_marks_plan_provisional():
    status = ready()
    status["reminders"]["available"] = False
    p = plan(status=status)
    assert p["provisional"] and p["blocks"] and not p["unscheduled"]


@pytest.mark.parametrize("seed", range(20))
def test_generated_schedules_respect_every_fixed_interval(seed):
    import random
    rng = random.Random(seed)
    events = []
    for i in range(12):
        start = at(9) + rng.randrange(0, 9 * 60) * 60
        events.append(event(str(i), start=start, end=start + rng.randrange(5, 91) * 60))
    tasks = [task(id=f"task-{i}", priority=rng.randrange(1, 4), duration_minutes=rng.randrange(5, 121),
                  due_ts=at(9) + rng.randrange(1, 10) * 3600) for i in range(20)]
    p = plan(tasks=tasks, commitments=events)
    assert p == plan(tasks=tasks, commitments=list(reversed(events)))
    flexible = [b for b in p["blocks"] if b["task_id"]]
    for b in flexible:
        original = next(t for t in tasks if t["id"] == b["task_id"])
        assert at(9) <= b["start"] < b["end"] <= min(at(18), original["due_ts"])
        assert b["end"] - b["start"] == original["duration_minutes"] * 60
        for other in p["blocks"]:
            if b["id"] != other["id"]:
                assert b["end"] <= other["start"] or b["start"] >= other["end"]
    assert len(flexible) + len(p["unscheduled"]) == len(tasks)


def test_sync_receipt_is_atomic_with_rows_and_failed_replace_rolls_back(store, monkeypatch):
    import service.assistant.store as sm
    monkeypatch.setattr(sm.time, "time", lambda: NOW)
    metadata = dict(snapshot_started_at=NOW-1, coverage_start=NOW-86400, coverage_end=NOW+86400)
    store.sync_source("calendar", [event()], diagnostics=metadata)
    before = store.today_snapshot(DAY, ZONE)
    assert before["sources"]["calendar"]["snapshot_started_at"] == NOW-1
    with pytest.raises(ValueError):
        store.sync_source("calendar", [event(end=at(8))], diagnostics=metadata | {"snapshot_started_at": NOW})
    assert store.today_snapshot(DAY, ZONE) == before


def test_older_native_snapshot_and_unavailable_receipt_cannot_replace_newer(store, monkeypatch):
    import service.assistant.store as sm
    monkeypatch.setattr(sm.time, "time", lambda: NOW)
    store.sync_source("calendar", [event("latest")], diagnostics=dict(snapshot_started_at=NOW))
    with pytest.raises(RevisionConflict):
        store.sync_source("calendar", [event("old")], diagnostics=dict(snapshot_started_at=NOW-1))
    with pytest.raises(RevisionConflict):
        store.today_source_unavailable("calendar", dict(snapshot_started_at=NOW-1, authorized=False))
    with pytest.raises(RevisionConflict):
        store.sync_source("calendar", [])
    assert store.today_snapshot(DAY, ZONE)["commitments"][0]["source_id"] == "latest"


def test_unavailable_receipt_preserves_rows_but_blocks_placement(store, monkeypatch):
    import service.assistant.store as sm
    monkeypatch.setattr(sm.time, "time", lambda: NOW)
    store.sync_source("calendar", [event()])
    store.today_source_unavailable("calendar", dict(authorized=False))
    state = store.today_snapshot(DAY, ZONE)
    p = plan(commitments=state["commitments"], status=state["sources"])
    assert p["unscheduled"] and not p["sources"]["calendar"]["ready"]
    assert len(state["commitments"]) == 1


def test_generic_delete_cleans_auxiliary_duration_immediately(store):
    store.sync_source("calendar", [event()])
    cid = store.today_snapshot(DAY, ZONE)["commitments"][0]["id"]
    assert store.delete(cid)
    assert store._db.execute("SELECT COUNT(*) FROM calendar_event_ends").fetchone()[0] == 0


def test_zero_duration_calendar_events_are_visible_but_not_busy(store):
    lo, _ = day_bounds(DAY, ZONE)
    store.sync_source("calendar", [event("point", start=at(9, 30), end=at(9, 30)),
                                   event("midnight", start=lo, end=lo)])
    p = plan(commitments=store.today_snapshot(DAY, ZONE)["commitments"])
    assert len(p["blocks"]) == 3 and not p["provisional"]
    flexible = next(b for b in p["blocks"] if b["task_id"])
    assert flexible["start"] == at(9) and not flexible["warnings"]
    assert all(not b["warnings"] for b in p["blocks"])


def test_zero_duration_event_does_not_break_native_ingestion(store, monkeypatch):
    import asyncio
    from service import main
    from unittest.mock import AsyncMock
    monkeypatch.setattr(main, "assistant_store", store)
    monkeypatch.setattr(main.assistant_hub, "publish", AsyncMock())
    monkeypatch.setattr(main.assistant_scheduler, "record_sync", lambda *a, **kw: None)
    result = asyncio.run(main.assistant_sync_calendar({"events": [event("zero", end=at(9)), event("normal")]}))
    assert result["synced"] == 2
    assert len(store.today_snapshot(DAY, ZONE)["commitments"]) == 2


def test_previous_day_pins_occupy_next_day_until_their_end(store):
    previous = "2026-09-23"
    lo, _ = day_bounds(DAY, ZONE)
    saved = store.today_save_task(dict(title="Late study", day=previous, timezone=ZONE,
        duration_minutes=120, pinned_start=lo - 3600))
    current = store.today_save_task(dict(title="Early project", day=DAY, timezone=ZONE, duration_minutes=60))
    state = store.today_snapshot(DAY, ZONE)
    p = build_plan(DAY, ZONE, state["tasks"], [], PREF | {"start_minute": 0}, ready(lo), now=lo)
    carried = next(b for b in p["blocks"] if b["task_id"] == saved["id"])
    placed = next(b for b in p["blocks"] if b["task_id"] == current["id"])
    assert carried["end"] == lo + 3600 and placed["start"] == carried["end"]
    assert "Continues from the previous day" in carried["warnings"]


def test_pin_ending_at_midnight_does_not_occupy_next_day(store):
    lo, _ = day_bounds(DAY, ZONE)
    store.today_save_task(dict(title="Late study", day="2026-09-23", timezone=ZONE,
        duration_minutes=60, pinned_start=lo - 3600))
    assert store.today_snapshot(DAY, ZONE)["tasks"] == []


def test_delayed_native_snapshot_does_not_look_fresh(store, monkeypatch):
    import service.assistant.store as sm
    monkeypatch.setattr(sm.time, "time", lambda: NOW)
    metadata = dict(snapshot_started_at=NOW-600, coverage_start=NOW-86400, coverage_end=NOW+86400)
    store.sync_source("calendar", [], diagnostics=metadata)
    state = store.today_snapshot(DAY, ZONE)
    assert state["sources"]["calendar"]["last_sync"] == NOW-600
    assert plan(status=state["sources"])["unscheduled"]


def test_ordinary_edit_of_pin_retains_original_timezone(store):
    pin = datetime(2026, 9, 24, 23, 30, tzinfo=ZoneInfo(ZONE)).timestamp()
    saved = store.today_save_task(dict(title="Late study", day=DAY, timezone=ZONE,
        duration_minutes=60, pinned_start=pin))
    edited = store.today_save_task(dict(title="Renamed", duration_minutes=90), task_id=saved["id"], revision=1)
    assert edited["timezone"] == ZONE and edited["pinned_start"] == pin


def test_native_today_contract_is_in_automated_gate(tmp_path):
    from scripts import run_simulation_qa as qa
    gates = qa._native_gates(tmp_path)
    commands = [command for name, command in gates if name == "native/today-contract"]
    assert commands == [[qa.TRUSTED_BASH, "scripts/test_today_contract.sh"]]


@pytest.mark.parametrize("old_diagnostics", [
    {"authorized": True},
    {"authorized": False},
    {"authorized": True, "available": False},
    {"authorized": False, "syncing": True},
])
def test_reminders_reverse_callbacks_preserve_deadline_and_receipt(store, monkeypatch, old_diagnostics):
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from fastapi import HTTPException
    from service import main
    import service.assistant.store as sm
    monkeypatch.setattr(sm.time, "time", lambda: NOW)
    monkeypatch.setattr(main, "assistant_store", store)
    publish, record = AsyncMock(), Mock()
    monkeypatch.setattr(main.assistant_hub, "publish", publish)
    monkeypatch.setattr(main.assistant_scheduler, "record_sync", record)
    # The second capture returns first with a deadline; the first later returns empty.
    asyncio.run(main.assistant_sync_calendar(dict(source="reminders", events=[
        dict(source_id="exam", title="Exam", kind="reminder", when_ts=at(12))],
        diagnostics=dict(authorized=True, snapshot_started_at=NOW-10))))
    before = store.today_snapshot(DAY, ZONE)
    monkeypatch.setattr(sm.time, "time", lambda: NOW+60)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.assistant_sync_calendar(dict(source="reminders", events=[],
            diagnostics=old_diagnostics | dict(snapshot_started_at=NOW-20))))
    assert error.value.status_code == 409
    assert store.today_snapshot(DAY, ZONE) == before
    assert before["commitments"][0]["source_id"] == "exam"
    assert before["sources"]["reminders"]["last_sync"] == NOW-10
    assert record.call_count == publish.await_count == 1


@pytest.mark.parametrize("diagnostics", [
    {"authorized": False},
    {"authorized": True, "available": False},
    {"authorized": False, "syncing": True},
])
def test_reminders_unavailable_capture_preserves_deadline_and_rejects_older_success(store, monkeypatch, diagnostics):
    import service.assistant.store as sm
    monkeypatch.setattr(sm.time, "time", lambda: NOW)
    store.sync_source("reminders", [dict(source_id="exam", title="Exam", kind="reminder", when_ts=at(12))],
                      diagnostics=dict(snapshot_started_at=NOW-700))
    store.today_source_unavailable("reminders", diagnostics | dict(snapshot_started_at=NOW-600))
    before = store.today_snapshot(DAY, ZONE)
    receipt = before["sources"]["reminders"]
    assert receipt["last_sync"] == receipt["snapshot_started_at"] == NOW-600
    assert not receipt["available"]
    assert not plan(status=before["sources"])["sources"]["reminders"]["ready"]
    with pytest.raises(RevisionConflict):
        store.sync_source("reminders", [], diagnostics=dict(snapshot_started_at=NOW-650))
    assert store.today_snapshot(DAY, ZONE) == before
    assert before["commitments"][0]["source_id"] == "exam"


def test_reminders_delayed_success_is_not_reported_current(store, monkeypatch):
    import service.assistant.store as sm
    monkeypatch.setattr(sm.time, "time", lambda: NOW)
    store.sync_source("reminders", [], diagnostics=dict(snapshot_started_at=NOW-600))
    state = store.today_snapshot(DAY, ZONE)
    assert state["sources"]["reminders"]["last_sync"] == NOW-600
    assert not plan(status=state["sources"])["sources"]["reminders"]["ready"]


def test_native_reminders_captures_time_before_fetch_for_every_receipt():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "app/Sources/WispApp/RemindersWriter.swift").read_text()
    sync = source.split("    func sync() {", 1)[1].split("    private func post", 1)[0]
    capture = "let snapshotStartedAt = Date().timeIntervalSince1970"
    assert sync.count(capture) == 1
    assert sync.index(capture) < sync.index("guard isAuthorized") < sync.index("store.fetchReminders")
    # All three outcomes (denied, nil fetch, success) carry the same captured value.
    assert sync.count('"snapshot_started_at": snapshotStartedAt') == 3
    assert sync.count("post(reminders:") == 3


def test_late_calendar_creation_receipt_keeps_newer_synced_duration_and_busy_time(store):
    import json
    payload = dict(type="create_calendar_event", action_id="late-create", title="Initial Meeting",
                   when_ts=at(9), duration_min=60, location="")
    store._db.execute("INSERT INTO assistant_events(id,kind,payload,dedupe_key,target,created_at,state,claim_token) "
                      "VALUES (?,?,?,?,?,?,'pending','late-claim')",
                      ("late-event", "create_calendar_event", json.dumps(payload), "action:late-create",
                       json.dumps({"type": "calendar"}), NOW))
    store._db.commit()
    store.sync_source("calendar", [event("native-id", start=at(9), end=at(12), title="Updated Meeting")])
    before = store.today_snapshot(DAY, ZONE)["commitments"][0]
    store.complete_calendar_action("late-event", "create_calendar_event", "late-claim",
                                   dict(ok=True, status="succeeded", source_id="native-id", error=""))
    after = store.today_snapshot(DAY, ZONE)["commitments"][0]
    assert after["id"] == before["id"]
    assert after["title"] == "Updated Meeting"
    assert after["end_ts"] == at(12)
    planned = plan(tasks=[task(duration_minutes=60)], commitments=[after])
    scheduled = next(block for block in planned["blocks"] if block["task_id"] == "t1")
    assert scheduled["start"] == at(12)


def test_cross_zone_pin_appears_only_on_days_its_interval_overlaps(store):
    la = ZoneInfo("America/Los_Angeles")
    ny = "America/New_York"
    start = datetime(2026, 9, 25, 23, 30, tzinfo=la).timestamp()
    saved = store.today_save_task(dict(title="Late study", day="2026-09-25", timezone=ZONE,
                                      duration_minutes=60, pinned_start=start))
    day = "2026-09-25"
    state = store.today_snapshot(day, ny)
    current = build_plan(day, ny, state["tasks"], state["commitments"], PREF, ready(start),
                         now=day_bounds(day, ny)[0])
    assert any(t["id"] == saved["id"] for t in current["tasks"])  # still editable
    assert not any(b["task_id"] == saved["id"] for b in current["blocks"])
    next_day = "2026-09-26"
    state = store.today_snapshot(next_day, ny)
    following = build_plan(next_day, ny, state["tasks"], state["commitments"], PREF,
                           ready(start), now=day_bounds(next_day, ny)[0])
    displayed = next(b for b in following["blocks"] if b["task_id"] == saved["id"])
    assert displayed["start"] == start and displayed["end"] == start + 3600
