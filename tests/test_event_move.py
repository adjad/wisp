"""update_event: a verified native move of one Calendar occurrence (2026-10-18).

The tool resolves the exact synced occurrence, asks the app to move only its start and
end (CalendarReader.moveEvent, rules in CalendarMove.swift), and reports success only on
the app's read-back. These cover the matching, the refusals, the payload, the receipt
the store records, and the confirmation card. No real Calendar or app is involved: the
native bridge is a mock.
"""
from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-event-move-"))

from service.assistant import outbox  # noqa: E402
from service.assistant.hub import hub  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402
from service.tools import assistant_tools  # noqa: E402
from service.tools.registry import EVENT_UPDATE_UNAVAILABLE, REGISTRY, classify_tool_outcome  # noqa: E402

HOUR = 3600.0


def _top_of_hour(offset_hours: float) -> float:
    base = int(time.time() // HOUR) * HOUR
    return base + offset_hours * HOUR


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(assistant_tools, "assistant_store", s)
    yield s
    s._db.close()


def _event(sid, title, start, length_h=1.0, **extra):
    return {"source_id": sid, "kind": "meeting", "title": title, "when_ts": start,
            "end_ts": start + length_h * HOUR, "context": "Home", **extra}


def _calendar(store, *events):
    store.sync_source("calendar", list(events))


@pytest.fixture()
def bridge(monkeypatch):
    """The app side: connected, and answering each move with a verified read-back."""
    calls = []

    async def request(kind, payload, timeout=None):
        calls.append((kind, payload))
        start = payload["when_ts"]
        length = payload["duration_min"] * 60 if payload["duration_min"] else HOUR
        return {"ok": True, "status": "succeeded", "error": "", "when_ts": start, "end_ts": start + length}

    monkeypatch.setattr(outbox, "request", request)
    monkeypatch.setattr(outbox, "app_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(hub, "publish", AsyncMock())
    return calls


# --- resolving the occurrence ------------------------------------------------------------

def test_exact_title_wins_and_only_calendar_rows_are_targets(store):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Team meeting", start), _event("ek-2", "Team meeting prep", start + 2 * HOUR))
    store.sync_source("reminders", [{"source_id": "r-1", "kind": "reminder", "title": "Team meeting",
                                     "when_ts": start}])
    found = store.calendar_move_target("team  MEETING")
    assert found["row"]["source_id"] == "ek-1" and found["row"]["recurring"] is False
    assert store.calendar_move_target("prep")["row"]["source_id"] == "ek-2"


def test_several_different_events_need_the_user_to_say_which(store):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Standup", start), _event("ek-2", "Standup", start + 24 * HOUR))
    found = store.calendar_move_target("standup")
    assert found["needs_input"] and "Which one?" in found["error"]
    picked = store.calendar_move_target("standup", current_start=start + 24 * HOUR)
    assert picked["row"]["source_id"] == "ek-2"


def test_a_repeating_event_resolves_to_its_next_occurrence(store):
    start = _top_of_hour(26)
    _calendar(store, *[_event("ek-series", "Standup", start + day * 24 * HOUR) for day in range(3)])
    row = store.calendar_move_target("standup")["row"]
    assert row["when_ts"] == start and row["recurring"] is True


def test_finished_events_and_unknown_titles_are_not_targets(store):
    _calendar(store, _event("ek-old", "Dentist", _top_of_hour(-30)))
    assert store.calendar_move_target("dentist")["needs_input"]
    assert store.calendar_move_target("")["error"] == "Which event should move?"


# --- the tool -----------------------------------------------------------------------------

async def test_a_move_sends_the_exact_occurrence_and_reports_the_read_back(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start, location="Main St"))
    new = start + 2 * HOUR
    result = await assistant_tools.update_event("dentist", _iso(new))
    assert bridge == [("move_calendar_event", {
        "source_id": "ek-1", "expected_title": "Dentist", "expected_start_ts": start,
        "when_ts": new, "duration_min": None})]
    assert result.startswith("Moved “Dentist” to ")
    assert classify_tool_outcome("update_event", result).status == "succeeded"
    hub.publish.assert_awaited()


async def test_a_new_length_is_sent_only_when_asked(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    await assistant_tools.update_event("dentist", _iso(start + HOUR), duration_min=30)
    assert bridge[-1][1]["duration_min"] == 30


@pytest.mark.parametrize("change", [{"location": "Elm St"}, {"new_title": "Dentist (moved)"}])
async def test_renaming_or_relocating_is_refused_before_anything_runs(store, bridge, change):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    result = await assistant_tools.update_event("dentist", _iso(start + HOUR), **change)
    assert "can't rename" in result and bridge == []


async def test_past_times_ambiguity_and_no_match_change_nothing(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Standup", start), _event("ek-2", "Standup", start + 24 * HOUR))
    assert "in the past" in await assistant_tools.update_event("standup", _iso(_top_of_hour(-2)))
    ambiguous = await assistant_tools.update_event("standup", _iso(start + HOUR))
    assert classify_tool_outcome("update_event", ambiguous).status == "needs_input"
    missing = await assistant_tools.update_event("yoga", _iso(start + HOUR))
    assert classify_tool_outcome("update_event", missing).status == "needs_input"
    assert bridge == []


async def test_the_app_being_down_is_the_unavailable_answer(store, bridge, monkeypatch):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    monkeypatch.setattr(outbox, "app_connected", AsyncMock(return_value=False))
    result = await assistant_tools.update_event("dentist", _iso(start + HOUR))
    assert result == EVENT_UPDATE_UNAVAILABLE and bridge == []
    assert classify_tool_outcome("update_event", result).status == "needs_input"


@pytest.mark.parametrize("native, says", [
    ({"ok": False, "status": "failed", "error": "Only the organizer can move an event with invitees"},
     "couldn't move"),
    ({"ok": False, "status": "unknown", "error": "read-back differs (notes); the outcome is unknown"},
     "not confirmed"),
])
async def test_refused_and_unverified_moves_are_never_success(store, monkeypatch, native, says):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    monkeypatch.setattr(outbox, "request", AsyncMock(return_value=native))
    monkeypatch.setattr(outbox, "app_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(hub, "publish", AsyncMock())
    result = await assistant_tools.update_event("dentist", _iso(start + HOUR))
    assert says in result
    assert classify_tool_outcome("update_event", result).status == "failed"
    hub.publish.assert_not_awaited()


def test_update_event_is_a_confirmed_calendar_write():
    from service.safety.policy import ALWAYS_CONFIRM_CATEGORIES
    tool = REGISTRY["update_event"]
    assert tool.category == "calendar_write" and tool.category in ALWAYS_CONFIRM_CATEGORIES
    assert not tool.unavailable_reason


def test_the_confirmation_card_names_the_occurrence_and_both_times(store):
    start = _top_of_hour(26)
    _calendar(store, *[_event("ek-series", "Standup", start + day * 24 * HOUR, 0.25) for day in range(2)])
    card = assistant_tools.event_move_preview({"title": "standup", "when_iso": _iso(start + HOUR)})
    assert card.startswith("Move: Standup — from ")
    assert "(this occurrence only)" in card and " to " in card


# --- payload, result and receipt ---------------------------------------------------------

def _payload(**over):
    return {"type": "move_calendar_event", "action_id": "move-1", "source_id": "ek-1",
            "expected_title": "Dentist", "expected_start_ts": 1_900_000_000.0,
            "when_ts": 1_900_007_200.0, "duration_min": None, **over}


def test_payload_validation():
    assert AssistantStore.calendar_payload("move_calendar_event", _payload())["duration_min"] is None
    for bad in (_payload(duration_min=0), _payload(duration_min=1.5), _payload(expected_title=" "),
                _payload(expected_start_ts=0), _payload(location="x")):
        with pytest.raises(ValueError):
            AssistantStore.calendar_payload("move_calendar_event", bad)


def test_a_verified_result_must_match_the_requested_move():
    payload = AssistantStore.calendar_payload("move_calendar_event", _payload(duration_min=30))
    ok = {"ok": True, "status": "succeeded", "error": "", "when_ts": 1_900_007_200.0, "end_ts": 1_900_009_000.0}
    assert AssistantStore.calendar_result("move_calendar_event", ok, payload)["end_ts"] == 1_900_009_000.0
    for bad in ({**ok, "when_ts": 1_900_010_800.0}, {**ok, "end_ts": 1_900_010_800.0},
                {k: v for k, v in ok.items() if k != "end_ts"}):
        with pytest.raises(ValueError):
            AssistantStore.calendar_result("move_calendar_event", bad, payload)
    unknown = {"ok": False, "status": "unknown", "error": "read-back differs"}
    assert AssistantStore.calendar_result("move_calendar_event", unknown, payload)["status"] == "unknown"


def test_the_receipt_moves_the_synced_row_and_resets_its_notices(store):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    cid = store._db.execute("SELECT id FROM commitments WHERE source_id='ek-1'").fetchone()["id"]
    store._db.execute("INSERT INTO notify_log(commitment_id,stage,sent_at) VALUES (?,?,?)", (cid, "T-30m", 1.0))
    store._db.commit()
    payload = _payload(source_id="ek-1", expected_start_ts=start, when_ts=start + 2 * HOUR)
    row = store.enqueue_event(payload, target={"type": "calendar"}, dedupe_key="action:move-1",
                              expires_at=time.time() + 60)
    claim = store.claim_calendar_action(row["id"], "move_calendar_event", "move-1", payload)
    assert claim["execute"] is True
    store.complete_calendar_action(row["id"], "move_calendar_event", claim["claim_token"], {
        "ok": True, "status": "succeeded", "error": "", "when_ts": start + 2 * HOUR, "end_ts": start + 3 * HOUR})
    moved = store._db.execute(
        "SELECT c.when_ts, e.end_ts FROM commitments c JOIN calendar_event_ends e ON e.commitment_id=c.id "
        "WHERE c.id=?", (cid,)).fetchone()
    assert (moved["when_ts"], moved["end_ts"]) == (start + 2 * HOUR, start + 3 * HOUR)
    assert store._db.execute("SELECT COUNT(*) FROM notify_log WHERE commitment_id=?", (cid,)).fetchone()[0] == 0
