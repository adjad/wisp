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
    # As the app syncs it: an invitee count, and organizer_is_user when Calendar says.
    return {"source_id": sid, "kind": "meeting", "title": title, "when_ts": start,
            "end_ts": start + length_h * HOUR, "context": "Home", "invitee_count": 0, **extra}


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

async def _move(title, when_iso, **extra):
    """update_event as the agent loop runs it: resolved once, then the bound call."""
    prepared = assistant_tools.prepare_event_move({"title": title, "when_iso": when_iso, **extra})
    if "error" in prepared:
        return prepared["error"]
    with assistant_tools.bound_event_move(prepared["binding"]):
        return await assistant_tools.update_event(**prepared["args"])


async def test_an_unbound_call_never_moves_anything(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    result = await assistant_tools.update_event("dentist", _iso(start + HOUR))
    assert "needs the user's confirmation" in result and bridge == []

async def test_a_move_sends_the_exact_occurrence_and_reports_the_read_back(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start, location="Main St"))
    new = start + 2 * HOUR
    result = await _move("dentist", _iso(new))
    assert bridge == [("move_calendar_event", {
        "source_id": "ek-1", "expected_title": "Dentist", "expected_start_ts": start,
        "when_ts": new, "duration_min": None, "invitee_count": 0})]
    assert result.startswith("Moved “Dentist” to ")
    assert classify_tool_outcome("update_event", result).status == "succeeded"
    hub.publish.assert_awaited()


async def test_a_new_length_is_sent_only_when_asked(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    await _move("dentist", _iso(start + HOUR), duration_min=30)
    assert bridge[-1][1]["duration_min"] == 30


@pytest.mark.parametrize("change", [{"location": "Elm St"}, {"new_title": "Dentist (moved)"}])
async def test_renaming_or_relocating_is_refused_before_anything_runs(store, bridge, change):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    result = await _move("dentist", _iso(start + HOUR), **change)
    assert "can't rename" in result and bridge == []


async def test_past_times_ambiguity_and_no_match_change_nothing(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Standup", start), _event("ek-2", "Standup", start + 24 * HOUR))
    assert "in the past" in await _move("standup", _iso(_top_of_hour(-2)))
    ambiguous = await _move("standup", _iso(start + HOUR))
    assert classify_tool_outcome("update_event", ambiguous).status == "needs_input"
    missing = await _move("yoga", _iso(start + HOUR))
    assert classify_tool_outcome("update_event", missing).status == "needs_input"
    assert bridge == []


async def test_the_app_being_down_is_the_unavailable_answer(store, bridge, monkeypatch):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    monkeypatch.setattr(outbox, "app_connected", AsyncMock(return_value=False))
    result = await _move("dentist", _iso(start + HOUR))
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
    result = await _move("dentist", _iso(start + HOUR))
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
    prepared = assistant_tools.prepare_event_move({"title": "standup", "when_iso": _iso(start + HOUR)})
    card = prepared["preview"]
    assert card.startswith("Move: Standup — from ")
    assert "(this occurrence only)" in card and " to " in card
    # The approved call carries the start pinned to this Mac's UTC offset.
    assert prepared["args"]["when_iso"] != _iso(start + HOUR) and "+" in prepared["args"]["when_iso"] or "-" in prepared["args"]["when_iso"][16:]
    assert prepared["binding"]["payload"]["expected_start_ts"] == start


# --- payload, result and receipt ---------------------------------------------------------

def _payload(**over):
    return {"type": "move_calendar_event", "action_id": "move-1", "source_id": "ek-1",
            "expected_title": "Dentist", "expected_start_ts": 1_900_000_000.0,
            "when_ts": 1_900_007_200.0, "duration_min": None, "invitee_count": 0, **over}


def test_payload_validation():
    assert AssistantStore.calendar_payload("move_calendar_event", _payload())["duration_min"] is None
    for bad in (_payload(duration_min=0), _payload(duration_min=1.5), _payload(expected_title=" "),
                _payload(expected_start_ts=0), _payload(location="x"), _payload(invitee_count=-1),
                _payload(invitee_count=True)):
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


# --- the agent loop: one resolution, bound from card to write (audit B1, 2026-10-18) ------

import json  # noqa: E402

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
        await loop.run_agent(_Client([("update_event", args), "Okay."]), "fixture-model",
                             [{"role": "user", "content": "move it"}], emit, approver,
                             tools=["update_event"], include_memory_context=False, max_steps=3)
    results = [c.args[0]["result"] for c in emit.await_args_list if c.args[0].get("type") == "tool_result"]
    return approver, results


async def test_the_card_shows_the_occurrence_the_approved_call_moves(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    seen = []

    async def confirm(action):
        seen.append(action)
        return True
    approver, results = await _turn({"title": "dentist", "when_iso": _iso(start + 2 * HOUR)}, confirm=confirm)
    assert seen and seen[0]["preview"].startswith("Move: Dentist — from ")
    assert [p for _, p in bridge] == [{"source_id": "ek-1", "expected_title": "Dentist",
                                       "expected_start_ts": start, "when_ts": start + 2 * HOUR,
                                       "duration_min": None, "invitee_count": 0}]
    assert results[-1].startswith("Moved “Dentist” to ")


async def test_a_change_while_the_card_is_open_never_moves_a_different_event(store, bridge):
    # Audit B1: the card resolved today's standup; by approval it had ended, and a fresh
    # resolution would have picked tomorrow's. The bound call moves only what was shown.
    start = _top_of_hour(26)
    _calendar(store, *[_event("ek-series", "Standup", start + day * 24 * HOUR, 0.5) for day in range(2)])

    async def confirm(action):
        # While the card is open, the shown occurrence disappears from Calendar.
        store.sync_source("calendar", [_event("ek-series", "Standup", start + 24 * HOUR, 0.5)])
        return True
    approver, results = await _turn({"title": "standup", "when_iso": _iso(start + HOUR)}, confirm=confirm)
    approver.confirm.assert_awaited_once()
    assert bridge == [], "tomorrow's occurrence must not move in its place"
    assert "changed after you confirmed" in results[-1]


async def test_a_move_that_cannot_resolve_raises_no_card(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Standup", start), _event("ek-2", "Standup", start + 24 * HOUR))
    approver, results = await _turn({"title": "standup", "when_iso": _iso(start + HOUR)},
                                    confirm=AsyncMock(return_value=True))
    approver.confirm.assert_not_awaited()
    assert "Which one?" in results[-1] and bridge == []


async def test_a_denied_card_moves_nothing(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    approver, _ = await _turn({"title": "dentist", "when_iso": _iso(start + HOUR)},
                              confirm=AsyncMock(return_value=False))
    approver.confirm.assert_awaited_once()
    assert bridge == []


def test_a_title_with_quote_marks_still_reads_as_success():
    assert classify_tool_outcome("update_event", "Moved “Sam’s “big” day” to Tue Oct 20, 3:00 PM–4:00 PM.").status == "succeeded"



# --- invitees: only the organizer moves them, and the card says they'll be told ----------

async def test_the_organizer_may_move_an_event_with_invitees_and_is_told_they_are_notified(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Design review", start, invitee_count=3, organizer_is_user=True))
    prepared = assistant_tools.prepare_event_move({"title": "design review", "when_iso": _iso(start + HOUR)})
    assert prepared["preview"].endswith("Its 3 invitees will be notified of the new time.")
    assert "@" not in prepared["preview"], "a count, never addresses"
    result = await _move("design review", _iso(start + HOUR))
    assert bridge[-1][1]["invitee_count"] == 3
    assert result.endswith("Invitees were notified of the new time.")
    assert classify_tool_outcome("update_event", result).status == "succeeded"


async def test_one_invitee_reads_in_the_singular(store, bridge):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "1:1", start, invitee_count=1, organizer_is_user=True))
    card = assistant_tools.prepare_event_move({"title": "1:1", "when_iso": _iso(start + HOUR)})["preview"]
    assert card.endswith("Its 1 invitee will be notified of the new time.")


@pytest.mark.parametrize("invitees, organizer, says", [
    (3, False, "Only the organizer"), (3, None, "who organizes"),
    # Audit B1: an invitation can list only the user as attendee (count 0); it is still
    # someone else's event.
    (0, False, "Only the organizer")])
async def test_a_non_organizer_or_unknown_organizer_is_refused(store, bridge, invitees, organizer, says):
    start = _top_of_hour(26)
    extra = {} if organizer is None else {"organizer_is_user": organizer}
    _calendar(store, _event("ek-1", "Design review", start, invitee_count=invitees, **extra))
    result = await _move("design review", _iso(start + HOUR))
    assert says in result and bridge == []
    assert "error" in assistant_tools.prepare_event_move({"title": "design review", "when_iso": _iso(start + HOUR)})


async def test_an_event_synced_without_invitee_data_is_refused(store, bridge):
    start = _top_of_hour(26)
    event = _event("ek-1", "Dentist", start)
    event.pop("invitee_count")
    _calendar(store, event)
    result = await _move("dentist", _iso(start + HOUR))
    assert "can't tell yet whether" in result and bridge == []


def test_invitee_metadata_follows_the_synced_row(store):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Design review", start, invitee_count=2, organizer_is_user=True))
    row = store.calendar_move_target("design review")["row"]
    assert (row["invitee_count"], row["organizer_is_user"]) == (2, 1)
    _calendar(store, _event("ek-1", "Design review", start))
    row = store.calendar_move_target("design review")["row"]
    assert (row["invitee_count"], row["organizer_is_user"]) == (0, None)
    _calendar(store)
    assert store._db.execute("SELECT COUNT(*) FROM calendar_event_invitees").fetchone()[0] == 0


def test_the_sync_endpoint_keeps_only_well_typed_invitee_fields(store, monkeypatch):
    from fastapi.testclient import TestClient
    import service.main as main
    monkeypatch.setattr(main, "assistant_store", store)
    monkeypatch.setattr(main.assistant_scheduler, "record_sync", lambda *a, **k: None)
    start = _top_of_hour(26)
    events = [_event("ek-1", "Review", start, invitee_count=2, organizer_is_user=True),
              _event("ek-2", "Sync", start + HOUR, invitee_count="2", organizer_is_user=1),
              _event("ek-3", "Retro", start + 2 * HOUR, invitee_count=-1)]
    response = TestClient(main.app).post("/assistant/sync/calendar", json={"events": events})
    assert response.status_code == 200
    rows = {r["title"]: (r["invitee_count"], r["organizer_is_user"]) for r in (
        store.calendar_move_target(t)["row"] for t in ("Review", "Sync", "Retro"))}
    assert rows == {"Review": (2, 1), "Sync": (None, None), "Retro": (None, None)}


# --- one-tap Cancel from the Day tab: a verified native cancel of one occurrence ---------

from fastapi.testclient import TestClient  # noqa: E402

from service.assistant import checkin, checkin_api  # noqa: E402


@pytest.fixture()
def cancel_client(store, monkeypatch):
    import service.main as main
    monkeypatch.setattr(checkin_api, "assistant_store", store)
    monkeypatch.setattr(hub, "publish", AsyncMock())
    monkeypatch.setattr(outbox, "app_connected", AsyncMock(return_value=True))
    calls = []

    async def request(kind, payload, timeout=None):
        calls.append((kind, payload))
        return {"ok": True, "status": "succeeded", "error": "", "is_absent": True}
    monkeypatch.setattr(outbox, "request", request)
    return TestClient(main.app), calls


def _cancel(client, start, sid="ek-1", invitees=0, headers=None):
    return client.post("/assistant/checkin/cancel_event", headers=headers,
                       json={"source_id": sid, "expected_start_ts": start, "expected_invitees": invitees})


def test_cancel_refuses_browser_pages(store, cancel_client):
    client, calls = cancel_client
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    assert _cancel(client, start, headers={"Origin": "https://example.invalid"}).status_code == 403
    form = client.post("/assistant/checkin/cancel_event", content=b'{"source_id": "ek-1"}',
                       headers={"Content-Type": "text/plain"})
    assert form.status_code in (415, 422) and calls == []


def test_cancel_sends_the_exact_occurrence_and_reports_it(store, cancel_client):
    client, calls = cancel_client
    start = _top_of_hour(26)
    _calendar(store, *[_event("ek-1", "Standup", start + day * 24 * HOUR, 0.25) for day in range(2)])
    response = _cancel(client, start + 24 * HOUR)
    assert response.status_code == 200, response.text
    assert response.json()["invitees_notified"] is False and response.json()["title"] == "Standup"
    assert calls == [("cancel_calendar_event", {"source_id": "ek-1", "expected_title": "Standup",
                                                "expected_start_ts": start + 24 * HOUR, "invitee_count": 0})]
    hub.publish.assert_awaited()


def test_the_organizer_cancels_an_event_with_invitees(store, cancel_client):
    client, calls = cancel_client
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Design review", start, invitee_count=3, organizer_is_user=True))
    response = _cancel(client, start, invitees=3)
    assert response.status_code == 200 and response.json()["invitees_notified"] is True
    assert response.json()["invitee_count"] == 3
    assert calls[-1][1]["invitee_count"] == 3


@pytest.mark.parametrize("extra, status, says", [
    ({"invitee_count": 3, "organizer_is_user": False}, 403, "Only the organizer can cancel"),
    ({"invitee_count": 0, "organizer_is_user": False}, 403, "Only the organizer can cancel"),
    ({"invitee_count": 3}, 403, "doesn't say who organizes"),
    ({"invitee_count": None}, 409, "hasn't synced"),
])
def test_cancel_refusals_send_nothing(store, cancel_client, extra, status, says):
    client, calls = cancel_client
    start = _top_of_hour(26)
    event = {**_event("ek-1", "Design review", start), **extra}
    if event["invitee_count"] is None:
        event.pop("invitee_count")
    _calendar(store, event)
    response = _cancel(client, start, invitees=extra.get("invitee_count") or 0)
    assert response.status_code == status and says in response.json()["detail"]["message"]
    assert calls == []


def test_cancel_needs_the_count_the_card_showed_and_a_synced_occurrence(store, cancel_client):
    client, calls = cancel_client
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Design review", start, invitee_count=3, organizer_is_user=True))
    assert _cancel(client, start, invitees=2).json()["detail"]["outcome"] == "not_current"
    assert _cancel(client, start + 60, invitees=3).status_code == 404
    assert calls == []


def test_cancel_without_the_app_is_unavailable(store, cancel_client, monkeypatch):
    client, calls = cancel_client
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    monkeypatch.setattr(outbox, "app_connected", AsyncMock(return_value=False))
    assert _cancel(client, start).status_code == 503 and calls == []


@pytest.mark.parametrize("native, status, outcome", [
    ({"ok": False, "status": "unknown", "error": "the outcome is unknown"}, 409, "unknown"),
    ({"ok": False, "status": "failed", "error": "That event changed since Wisp last read Calendar; nothing was cancelled"},
     409, "not_current"),
    ({"ok": False, "status": "failed", "error": "Calendar didn't cancel it: denied"}, 502, "failed"),
    ({"ok": False, "status": "failed", "error": "Only the organizer can cancel this event; decline it"}, 403, "not_allowed"),
    ({"ok": False, "error": "Wisp app is not connected; Calendar was not changed"}, 503, "unavailable"),
])
def test_cancel_native_outcomes_map_to_statuses(store, cancel_client, monkeypatch, native, status, outcome):
    client, _ = cancel_client
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    monkeypatch.setattr(outbox, "request", AsyncMock(return_value=native))
    response = _cancel(client, start)
    assert response.status_code == status and response.json()["detail"]["outcome"] == outcome
    hub.publish.assert_not_awaited()


def test_cancel_payload_result_and_receipt(store):
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Dentist", start))
    payload = {"type": "cancel_calendar_event", "action_id": "cancel-1", "source_id": "ek-1",
               "expected_title": "Dentist", "expected_start_ts": start, "invitee_count": 0}
    assert AssistantStore.calendar_payload("cancel_calendar_event", payload)["expected_start_ts"] == start
    for bad in ({**payload, "invitee_count": -1}, {**payload, "when_ts": start},
                {k: v for k, v in payload.items() if k != "expected_title"}):
        with pytest.raises(ValueError):
            AssistantStore.calendar_payload("cancel_calendar_event", bad)
    with pytest.raises(ValueError):
        AssistantStore.calendar_result("cancel_calendar_event", {"ok": True, "status": "succeeded", "error": ""}, payload)
    row = store.enqueue_event(payload, target={"type": "calendar"}, dedupe_key="action:cancel-1",
                              expires_at=time.time() + 60)
    claim = store.claim_calendar_action(row["id"], "cancel_calendar_event", "cancel-1", payload)
    store.complete_calendar_action(row["id"], "cancel_calendar_event", claim["claim_token"],
                                   {"ok": True, "status": "succeeded", "error": "", "is_absent": True})
    assert store.calendar_occurrence("ek-1", start) is None


def test_card_rows_carry_what_cancel_needs(store):
    from datetime import timezone as tz
    start = _top_of_hour(26)
    _calendar(store, _event("ek-1", "Design review", start, invitee_count=2, organizer_is_user=False),
              _event("ek-2", "Dentist", start + 60))
    day = datetime.fromtimestamp(start, tz.utc).strftime("%Y-%m-%d")
    rows = {r["source_id"]: r for r in checkin._event_rows(store, day, "UTC")}
    assert "Only the organizer" in rows["ek-1"]["cancel_blocked"]
    assert (rows["ek-1"]["invitee_count"], rows["ek-1"]["organizer_is_user"]) == (2, False)
    assert rows["ek-2"]["cancel_blocked"] == "" and rows["ek-2"]["invitee_count"] == 0
