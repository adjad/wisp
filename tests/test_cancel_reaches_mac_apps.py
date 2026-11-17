"""Cancelling in Harbor cancels in the Mac app too, and says so only when it did.

Ask's cancel_event (and everything that shares its removal path: clear_reminders,
clear_past_reminders, the Caught-for-you Undo, DELETE /assistant/commitments) now
cancels a Calendar event through the app's verified one-occurrence cancel, the same
one the Day tab's Cancel uses: identifier AND start, title and invitee count as
preconditions, never a read-only calendar or someone else's event, read back as gone.
A reminder is deleted in Apple Reminders by its exact identity; a Wisp-only item is
deleted locally. The confirmation card names the exact item, and the approved call
removes only that item.

Every native call is a stub: no Calendar, Reminders, Notes, Keychain or model is
reached. All names and values are invented.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-cancel-native-"))

from service.assistant import outbox  # noqa: E402
from service.assistant.hub import hub  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402
from service.tools import assistant_tools  # noqa: E402
from service.tools.registry import REGISTRY, classify_tool_outcome  # noqa: E402

HOUR = 3600.0


def _top_of_hour(offset_hours: float) -> float:
    return int(time.time() // HOUR) * HOUR + offset_hours * HOUR


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(assistant_tools, "assistant_store", s)
    yield s
    s._db.close()


def _event(sid, title, start, **extra):
    return {"source_id": sid, "kind": "meeting", "title": title, "when_ts": start,
            "end_ts": start + HOUR, "context": "Home", "invitee_count": 0,
            "calendar_writable": True, **extra}


class _Calls(list):
    replies: dict


@pytest.fixture()
def bridge(monkeypatch):
    """The app side: connected, and answering each native write with a verified result."""
    calls = _Calls()
    replies = calls.replies = {}

    async def request(kind, payload, timeout=None, **kwargs):
        calls.append((kind, payload))
        if kind in replies:
            return replies[kind]
        return {"ok": True, "status": "succeeded", "error": "", "is_absent": True}

    monkeypatch.setattr(outbox, "request", request)
    monkeypatch.setattr(outbox, "app_connected", AsyncMock(return_value=True))
    monkeypatch.setattr(hub, "publish", AsyncMock())
    return calls


# --- Calendar ------------------------------------------------------------------------------

async def test_a_calendar_event_is_cancelled_through_the_verified_native_path(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-fixture-1", "Pottery class", start)])
    result = await assistant_tools.cancel_event("pottery")
    assert bridge == [("cancel_calendar_event", {
        "source_id": "ek-fixture-1", "expected_title": "Pottery class",
        "expected_start_ts": start, "invitee_count": 0})]
    assert result.startswith("Cancelled “Pottery class” (")
    assert "removed from Calendar" in result
    assert classify_tool_outcome("cancel_event", result).status == "succeeded"
    hub.publish.assert_awaited()


async def test_the_old_unverified_delete_is_never_used(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-fixture-1", "Pottery class", start)])
    await assistant_tools.cancel_event("Pottery class")
    assert [kind for kind, _ in bridge] == ["cancel_calendar_event"]


async def test_a_repeating_event_cancels_only_its_next_occurrence(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-series", "Choir rehearsal", start + d * 7 * 24 * HOUR)
                                   for d in range(3)])
    plan = assistant_tools.cancel_plan("choir rehearsal")
    assert plan["recurring"] is True
    assert "this occurrence only; the rest of the series stays" in plan["preview"]
    result = await assistant_tools.cancel_event("choir rehearsal")
    assert [p["expected_start_ts"] for _, p in bridge] == [start]
    assert "Only this occurrence was removed; the rest of the series is unchanged." in result


async def test_a_given_time_picks_one_occurrence(store, bridge):
    start = _top_of_hour(30)
    later = start + 7 * 24 * HOUR
    store.sync_source("calendar", [_event("ek-series", "Choir rehearsal", start),
                                   _event("ek-series", "Choir rehearsal", later)])
    await assistant_tools.cancel_event("choir rehearsal", when_iso=_iso(later))
    assert [p["expected_start_ts"] for _, p in bridge] == [later]


async def test_someone_elses_invitation_is_never_cancelled(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-invite", "Planning sync", start,
                                          invitee_count=3, organizer_is_user=False)])
    result = await assistant_tools.cancel_event("planning sync")
    assert bridge == []
    assert "Only the organizer can cancel" in result and "decline it in Calendar" in result
    assert "Nothing was cancelled" in result
    assert classify_tool_outcome("cancel_event", result).status != "succeeded"


async def test_unknown_organizer_with_invitees_is_refused(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-unknown", "Garden committee", start, invitee_count=2)])
    result = await assistant_tools.cancel_event("garden committee")
    assert bridge == [] and "who organizes" in result


async def test_the_organizer_cancels_and_is_told_invitees_were_sent_it(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-own", "Book club", start,
                                          invitee_count=2, organizer_is_user=True)])
    plan = assistant_tools.cancel_plan("book club")
    assert "Its 2 invitees will be sent the cancellation." in plan["preview"]
    result = await assistant_tools.cancel_event("book club")
    assert bridge[0][1]["invitee_count"] == 2
    assert "Calendar sent its invitees the cancellation." in result


async def test_a_read_only_calendar_is_refused(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-sub", "Harvest fair", start, calendar_writable=False)])
    result = await assistant_tools.cancel_event("harvest fair")
    assert bridge == [] and "read-only calendar" in result


async def test_a_google_web_event_is_refused(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("gcal:fixture-1", "Tide walk", start)])
    result = await assistant_tools.cancel_event("tide walk")
    assert bridge == [] and "Nothing was cancelled" in result


async def test_unsynced_invitees_are_sent_as_none_for_the_app_to_verify(store, bridge):
    # An event Wisp just created has no invitee data until the next sync. The app
    # compares the count with the live event and refuses on any difference.
    start = _top_of_hour(30)
    store.sync_source("calendar", [{"source_id": "ek-new", "kind": "event", "title": "Bike tune-up",
                                    "when_ts": start, "context": "Home"}])
    await assistant_tools.cancel_event("bike tune-up")
    assert bridge == [("cancel_calendar_event", {"source_id": "ek-new", "expected_title": "Bike tune-up",
                                                 "expected_start_ts": start, "invitee_count": 0})]


@pytest.mark.parametrize("reply,says", [
    ({"ok": False, "status": "failed", "error": "That event changed since Wisp last read Calendar; nothing was cancelled"},
     "changed since"),
    ({"ok": False, "status": "unknown", "error": "Calendar accepted the cancel but the event still reads back"},
     "did not confirm whether"),
    ({"ok": False, "error": "Wisp app is not connected; Calendar was not changed"}, "not connected"),
])
async def test_an_unverified_native_result_is_never_reported_as_cancelled(store, bridge, reply, says):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-fixture-1", "Pottery class", start)])
    bridge.replies["cancel_calendar_event"] = reply
    result = await assistant_tools.cancel_event("pottery")
    assert not result.startswith("Cancelled") and says in result
    assert classify_tool_outcome("cancel_event", result).status == "failed"
    assert store.active_between()[0]["status"] == "active"


async def test_an_exact_title_wins_over_a_partial_one(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-a", "Standup", start),
                                   _event("ek-b", "Standup prep", start + 2 * HOUR)])
    await assistant_tools.cancel_event("standup")
    assert [p["source_id"] for _, p in bridge] == ["ek-a"]


async def test_different_same_titled_items_are_asked_about(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-a", "Swim lesson", start),
                                   _event("ek-b", "Swim lesson", start + 26 * HOUR)])
    result = await assistant_tools.cancel_event("swim lesson")
    assert bridge == [] and "Which one?" in result
    assert classify_tool_outcome("cancel_event", result).status in {"needs_input", "failed"}


# --- Reminders and Wisp-only ----------------------------------------------------------------

async def test_a_reminder_is_deleted_in_apple_reminders_by_its_identity(store, bridge):
    due = _top_of_hour(30)
    store.sync_source("reminders", [{"source_id": "rem-fixture-1", "kind": "reminder",
                                     "title": "Return library books", "when_ts": due,
                                     "context": "Errands"}])
    plan = assistant_tools.cancel_plan("library books")
    assert plan["preview"].startswith("Delete: Return library books — ")
    assert "from Apple Reminders (list “Errands”)" in plan["preview"]
    result = await assistant_tools.cancel_event("library books")
    assert [k for k, _ in bridge] == ["delete_reminder"]
    payload = bridge[0][1]
    assert payload["source_id"] == "rem-fixture-1" and payload["expected_title"] == "Return library books"
    assert payload["expected_due_ts"] == due
    assert "deleted from Apple Reminders" in result


async def test_a_reminder_the_app_cannot_delete_stays(store, bridge):
    due = _top_of_hour(30)
    store.sync_source("reminders", [{"source_id": "rem-fixture-1", "kind": "reminder",
                                     "title": "Return library books", "when_ts": due}])
    bridge.replies["delete_reminder"] = {"ok": False, "status": "failed", "error": "Reminders access denied"}
    result = await assistant_tools.cancel_event("library books")
    assert "Reminders access denied" in result and not result.startswith("Cancelled")
    assert store.active_between()[0]["status"] == "active"


async def test_a_wisp_only_reminder_is_removed_locally_and_says_so(store, bridge):
    row = store.add_manual("Water the ferns", _top_of_hour(30))
    result = await assistant_tools.cancel_event("ferns")
    assert bridge == [] and store.get(row["id"]) is None
    assert "no Calendar or Apple Reminders copy" in result


# --- The confirmation card and the approved call ------------------------------------------

import service.tools  # noqa: E402,F401
from service.agent import loop  # noqa: E402


class _Client:
    def __init__(self, replies):
        self.replies = iter(replies)

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        reply = next(self.replies, "Done.")
        if isinstance(reply, tuple):
            name, arguments = reply
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        else:
            message = {"role": "assistant", "content": reply}
        yield {"kind": "final", "message": message}


async def _turn(args, *, confirm):
    approver = type("Approver", (), {"confirm": AsyncMock(side_effect=confirm)})()
    emit = AsyncMock()
    with patch.object(loop, "audit"):
        await loop.run_agent(_Client([("cancel_event", args), "Okay."]), "fixture-model",
                             [{"role": "user", "content": "cancel it"}], emit, approver,
                             tools=["cancel_event"], include_memory_context=False, max_steps=3)
    results = [c.args[0]["result"] for c in emit.await_args_list if c.args[0].get("type") == "tool_result"]
    return approver, results


def test_cancel_event_stays_an_always_confirmed_calendar_write():
    assert REGISTRY["cancel_event"].category == "calendar_write"
    from service.safety.policy import Tier, decide
    assert decide("calendar_write", {"title": "x"}, "cancel_event").tier is Tier.CONFIRM


async def test_the_card_names_the_exact_event_and_the_approved_call_cancels_it(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-fixture-1", "Pottery class", start)])
    seen = []

    async def confirm(action):
        seen.append(action)
        return True
    approver, results = await _turn({"title": "pottery"}, confirm=confirm)
    when = datetime.fromtimestamp(start).strftime("%a %b %-d, %Y %-I:%M %p")
    assert seen and seen[0]["preview"] == f"Cancel: Pottery class — {when}, from Calendar (calendar “Home”)."
    assert [k for k, _ in bridge] == ["cancel_calendar_event"]
    assert results[-1].startswith("Cancelled “Pottery class”")


async def test_a_change_while_the_card_is_open_cancels_nothing(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-a", "Pottery class", start)])

    async def confirm(action):
        # While the card is open the shown event is replaced by another same-titled one.
        store.sync_source("calendar", [_event("ek-b", "Pottery class", start + 3 * HOUR)])
        return True
    approver, results = await _turn({"title": "pottery"}, confirm=confirm)
    approver.confirm.assert_awaited_once()
    assert bridge == [] and "changed after you confirmed" in results[-1]


async def test_an_item_that_cannot_be_cancelled_raises_no_card(store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-invite", "Planning sync", start,
                                          invitee_count=3, organizer_is_user=False)])
    approver, results = await _turn({"title": "planning sync"}, confirm=AsyncMock(return_value=True))
    approver.confirm.assert_not_awaited()
    assert bridge == [] and "Only the organizer" in results[-1]


async def test_a_denied_card_cancels_nothing(store, bridge):
    store.sync_source("calendar", [_event("ek-a", "Pottery class", _top_of_hour(30))])
    approver, _ = await _turn({"title": "pottery"}, confirm=AsyncMock(return_value=False))
    approver.confirm.assert_awaited_once()
    assert bridge == []


# --- The app's commitment endpoints -----------------------------------------------------------

@pytest.fixture()
def main_world(store, monkeypatch):
    from service import main
    monkeypatch.setattr(main, "assistant_store", store)
    monkeypatch.setattr(main, "assistant_hub", hub)
    return main


async def test_the_delete_endpoint_uses_the_verified_cancel(main_world, store, bridge):
    start = _top_of_hour(30)
    store.sync_source("calendar", [_event("ek-fixture-1", "Pottery class", start)])
    row = store.active_between()[0]
    assert (await main_world.assistant_delete(row["id"]))["ok"] is True
    assert [k for k, _ in bridge] == ["cancel_calendar_event"]


async def test_the_delete_endpoint_reports_a_refusal(main_world, store, bridge):
    store.sync_source("calendar", [_event("ek-invite", "Planning sync", _top_of_hour(30),
                                          invitee_count=1, organizer_is_user=False)])
    row = store.active_between()[0]
    answer = await main_world.assistant_delete(row["id"])
    assert answer["ok"] is False and "organizer" in answer["error"] and bridge == []


async def test_hiding_a_native_item_locally_is_refused(main_world, store, bridge):
    store.sync_source("calendar", [_event("ek-fixture-1", "Pottery class", _top_of_hour(30))])
    row = store.active_between()[0]
    answer = await main_world.assistant_update(row["id"], {"status": "dismissed"})
    assert answer["ok"] is False and store.get(row["id"])["status"] == "active" and bridge == []


async def test_marking_a_reminder_done_completes_it_in_reminders(main_world, store, bridge):
    store.sync_source("reminders", [{"source_id": "rem-fixture-1", "kind": "reminder",
                                     "title": "Return library books", "when_ts": _top_of_hour(30)}])
    row = store.active_between()[0]
    answer = await main_world.assistant_update(row["id"], {"status": "done"})
    assert answer["ok"] is True and [k for k, _ in bridge] == ["complete_reminder"]
    assert store.get(row["id"])["status"] == "done"
