"""Ask shows a fix button when a tool couldn't read a source for lack of access.

The signal rides on the tool's own tool_result event (`needs_access`), never on
what the model says, and the tool's text names the exact place to allow it. No
model, native bridge, network or user cache is used.
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from service.tools import access_needed


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "wisp"))
    monkeypatch.setenv("MOE_DIR", str(tmp_path / "moe"))

    def forbidden(*args, **kwargs):
        raise AssertionError("No real model, native process or network in access-fix tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def test_only_a_named_fixable_kind_is_noted():
    noted, _ = access_needed.start()
    assert "Privacy & Security > Calendars" in access_needed.need("calendar", "calendar_access")
    assert access_needed.need("calendar", "calendar_access")  # said again, noted once
    assert access_needed.need("calendar", "") == ""  # a passing failure offers no button
    assert access_needed.need("messages", "unreadable") == ""
    assert noted == [{"source": "calendar", "kind": "calendar_access"}]
    assert access_needed.drain(noted) == [{"source": "calendar", "kind": "calendar_access"}]
    assert noted == [] and access_needed.drain(noted) == []


def test_every_fix_names_an_exact_place():
    for kind in access_needed.FIXABLE:
        fix = access_needed.need("x", kind)
        assert fix.endswith(".") and ("System Settings >" in fix), kind
    # Calendar, Reminders, Contacts and Mail can also be allowed from Wisp's own step.
    for kind in ("calendar_access", "reminders_access", "contacts_access", "mail_automation"):
        assert "Wisp's Settings > Privacy > Permissions…" in access_needed.need("x", kind)


def _readiness(*rows):
    return {"sources": list(rows), "reminders_fresh": True}


def test_calendar_history_names_the_setting_and_notes_the_kind():
    from service.assistant import sync_status
    from service.tools import assistant_tools
    noted, _ = access_needed.start()
    row = {"id": "calendar", "label": "Calendar", "state": "unavailable", "reason_kind": "calendar_access"}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock, return_value=_readiness(row)):
        answer = asyncio.run(assistant_tools.get_past_events())
    assert answer.startswith("Wisp could not check Calendar. ")
    assert "System Settings > Privacy & Security > Calendars" in answer and answer.endswith("Then try again.")
    assert noted == [{"source": "calendar", "kind": "calendar_access"}]


def test_an_unexplained_calendar_failure_keeps_the_general_pointer_and_no_button():
    from service.assistant import sync_status
    from service.tools import assistant_tools
    noted, _ = access_needed.start()
    row = {"id": "calendar", "label": "Calendar", "state": "unavailable", "reason_kind": ""}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock, return_value=_readiness(row)):
        answer = asyncio.run(assistant_tools.get_past_events())
    assert answer == "Wisp could not check Calendar. Check its access in Settings. Then try again."
    assert noted == []


def test_schedule_notice_names_each_unreadable_source():
    from service.assistant import sync_status
    from service.tools import assistant_tools
    noted, _ = access_needed.start()
    rows = ({"id": "calendar", "label": "Calendar", "state": "unavailable", "reason_kind": "calendar_access"},
            {"id": "reminders", "label": "Reminders", "state": "unavailable", "reason_kind": "reminders_access"})
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock, return_value=_readiness(*rows)), \
         patch.object(assistant_tools.assistant_store, "upcoming", return_value=[]), \
         patch.object(assistant_tools.assistant_store, "calendar_events_overlapping", return_value=[]):
        answer = asyncio.run(assistant_tools.get_upcoming())
    assert answer.startswith("Wisp could not check Calendar and Reminders. ")
    assert "> Calendars." in answer and "> Reminders." in answer and "This schedule may be incomplete." in answer
    assert [n["kind"] for n in noted] == ["calendar_access", "reminders_access"]


def test_reminders_search_names_the_setting():
    from service.assistant import sync_status
    from service.tools import assistant_tools
    noted, _ = access_needed.start()
    row = {"id": "reminders", "label": "Reminders", "state": "unavailable", "reason_kind": "reminders_access"}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock, return_value=_readiness(row)), \
         patch.object(assistant_tools, "reminders_matching", return_value=[]):
        answer = asyncio.run(assistant_tools.search_reminders("dentist"))
    assert "could not check Apple Reminders" in answer and "Privacy & Security > Reminders" in answer
    assert noted == [{"source": "reminders", "kind": "reminders_access"}]


def test_contacts_not_allowed_is_said_and_noted(monkeypatch):
    from service.tools import imessage_tools as M
    monkeypatch.setattr(M, "_contacts_not_allowed", True)
    monkeypatch.setattr(M, "_name_handles", {})
    noted, _ = access_needed.start()
    answer = asyncio.run(M.lookup_contact("Mom"))
    assert "Wisp can't read Contacts yet." in answer and "Privacy & Security > Contacts" in answer
    assert noted == [{"source": "contacts", "kind": "contacts_access"}]
    monkeypatch.setattr(M, "_contacts_not_allowed", False)
    assert "first sync hasn't finished" in asyncio.run(M.lookup_contact("Mom"))


def test_a_contacts_read_that_was_refused_is_remembered():
    from service.tools import imessage_tools as M
    with patch.object(M, "clear_contacts"), patch.object(M, "cache_contacts"), patch.object(M, "cache_birthdays"):
        M.apply_contacts_sync({"contacts_enabled": True, "contacts_available": False})
        assert M._contacts_not_allowed
        M.apply_contacts_sync({"contacts_enabled": True, "contacts_available": True,
                               "contacts": {}, "birthdays": {}})
        assert not M._contacts_not_allowed
        # Turned off in Wisp is the person's choice, not a missing permission.
        M.apply_contacts_sync({"contacts_enabled": False, "contacts_available": False})
        assert not M._contacts_not_allowed


def test_the_tool_result_carries_the_fix_whatever_the_model_says(monkeypatch):
    from service.agent import loop
    from service.safety import grants, policy
    from service.tools.registry import REGISTRY
    from tests.test_direct_dispatch_exec import Approver, ScriptedClient, _tool_call
    monkeypatch.setattr(loop, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(grants, "check", lambda *a, **kw: None)

    async def calendar_denied(**kwargs):
        return ("Wisp could not check Calendar. " + access_needed.need("calendar", "calendar_access")
                + " Then try again.")

    async def calendar_fine(**kwargs):
        return "Today is Monday. Nothing scheduled in today."
    monkeypatch.setitem(REGISTRY, "get_past_events", replace(REGISTRY["get_past_events"], func=calendar_denied))
    monkeypatch.setitem(REGISTRY, "get_upcoming", replace(REGISTRY["get_upcoming"], func=calendar_fine))
    # The model's reply never mentions access: the button must not need it to.
    client = ScriptedClient([
        {"tool_calls": [_tool_call("get_past_events", {}, "denied-1")]},
        {"tool_calls": [_tool_call("get_upcoming", {}, "fine-1")]},
        {"content": "I couldn't look at that just now."},
        {"content": "I couldn't look at that just now."}])
    events = []

    async def emit(event):
        events.append(event)
    asyncio.run(loop.run_agent(client, "Agents-A1-4B-oQe6",
                               [{"role": "user", "content": "What did I have last week?"}], emit, Approver(True),
                               tools=["get_past_events", "get_upcoming"], max_steps=4,
                               include_memory_context=False, allow_skill_context=False))
    results = {e["id"]: e for e in events if e["type"] == "tool_result"}
    assert results["denied-1"]["needs_access"] == [{"source": "calendar", "kind": "calendar_access"}]
    # Drained once: a later call in the same turn carries nothing it didn't note.
    assert "needs_access" not in results.get("fine-1", {})


def test_a_finished_turn_hands_the_collector_back(monkeypatch):
    """run_agent resets what it set: a read after the turn, in the caller's own
    context, notes into the caller's collector (or none), never the turn's."""
    from service.agent import loop
    from service.safety import grants, policy
    from service.tools.registry import REGISTRY
    from tests.test_direct_dispatch_exec import Approver, ScriptedClient, _tool_call
    monkeypatch.setattr(loop, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(grants, "check", lambda *a, **kw: None)

    async def calendar_denied(**kwargs):
        return "Wisp could not check Calendar. " + access_needed.need("calendar", "calendar_access")
    monkeypatch.setitem(REGISTRY, "get_upcoming", replace(REGISTRY["get_upcoming"], func=calendar_denied))
    events = []

    async def emit(event):
        events.append(event)

    async def turn_then_read():
        outer, token = access_needed.start()
        try:
            await loop.run_agent(ScriptedClient([
                {"tool_calls": [_tool_call("get_upcoming", {}, "denied-1")]},
                {"content": "I couldn't look at that just now."},
                {"content": "I couldn't look at that just now."}]), "Agents-A1-4B-oQe6",
                [{"role": "user", "content": "What's on today?"}], emit, Approver(True),
                tools=["get_upcoming"], max_steps=3, include_memory_context=False, allow_skill_context=False)
            access_needed.need("reminders", "reminders_access")
            return outer
        finally:
            access_needed.finish(token)
    assert asyncio.run(turn_then_read()) == [{"source": "reminders", "kind": "reminders_access"}]
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[0]["needs_access"] == [{"source": "calendar", "kind": "calendar_access"}]

    async def bare():
        noted, token = access_needed.start()
        access_needed.finish(token)
        access_needed.need("calendar", "calendar_access")
        return noted
    assert asyncio.run(bare()) == [], "finish leaves no collector behind"


def test_a_note_belongs_to_the_one_call_that_made_it():
    outer, _ = access_needed.start()
    with access_needed.noting() as one:
        access_needed.need("calendar", "calendar_access")
    with access_needed.noting() as two:
        pass
    assert one == [{"source": "calendar", "kind": "calendar_access"}] and two == [] and outer == []
    assert access_needed.payload(one) == {"needs_access": one} and access_needed.payload(two) == {}
    # Back on the turn's collector once the call is done.
    access_needed.need("reminders", "reminders_access")
    assert outer == [{"source": "reminders", "kind": "reminders_access"}]
    # A call's own note is kept when the turn adds one it collected.
    event = access_needed.merged({"type": "tool_result", "needs_access": list(one)}, outer)
    assert [n["kind"] for n in event["needs_access"]] == ["calendar_access", "reminders_access"]
    assert outer == []


def test_the_direct_read_path_carries_the_fix(monkeypatch):
    """'What's on my calendar today?' is answered by execute_read with no model
    and no run_agent: its tool_result must still carry the button."""
    from service.tools.registry import REGISTRY
    from service.workflows.reads import execute_read

    async def calendar_denied(**kwargs):
        return ("Wisp could not check Calendar. " + access_needed.need("calendar", "calendar_access")
                + " Then try again.")
    monkeypatch.setitem(REGISTRY, "get_upcoming", replace(REGISTRY["get_upcoming"], func=calendar_denied))
    monkeypatch.setitem(REGISTRY, "get_past_events", replace(REGISTRY["get_past_events"], func=AsyncMock(
        return_value="Last week: nothing scheduled.")))
    events = []

    async def emit(event):
        events.append(event)
    asyncio.run(execute_read(([("get_upcoming", {}), ("get_past_events", {})], ""), emit))
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[0]["needs_access"] == [{"source": "calendar", "kind": "calendar_access"}]
    assert "needs_access" not in results[1]


def test_the_task_executor_carries_the_fix():
    from service.tasks.compiler import compile_reminder_create
    from service.tasks.executor import execute_task
    from service.tasks.planner import plan_task
    from service.tools.registry import REGISTRY, Tool
    from datetime import datetime
    plan = compile_reminder_create("remind me tomorrow to pack", now=datetime(2026, 9, 3, 10, 0))
    plan_task(plan)
    plan.status = "running"

    async def reminders_denied(**kwargs):
        return ("Wisp could not save that to Apple Reminders. "
                + access_needed.need("reminders", "reminders_access"))
    original = REGISTRY["add_reminder"]
    REGISTRY["add_reminder"] = Tool("add_reminder", "fixture", original.parameters, "assistant_write",
                                    reminders_denied)
    events = []

    async def emit(event):
        events.append(event)
    try:
        asyncio.run(execute_task(plan, emit, type("Approver", (), {"confirm": AsyncMock(return_value=True)})()))
    finally:
        REGISTRY["add_reminder"] = original
    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["needs_access"] == [{"source": "reminders", "kind": "reminders_access"}]


def test_contacts_waiting_on_messages_names_full_disk_access(monkeypatch):
    """The setup window offers Contacts only once Messages can be read, so
    'Click Allow next to Contacts' would point at a button that isn't there."""
    from service.tools import imessage_tools as M
    monkeypatch.setattr(M, "_contacts_not_allowed", True)
    monkeypatch.setattr(M, "_name_handles", {})
    monkeypatch.setattr(M, "messages_unavailable_kind", lambda: "full_disk_access")
    noted, _ = access_needed.start()
    answer = asyncio.run(M.lookup_contact("Mom"))
    assert "Wisp can't read Contacts yet" in answer and "Privacy & Security > Full Disk Access" in answer
    assert "Allow next to Contacts" not in answer
    assert noted == [{"source": "messages", "kind": "full_disk_access"}]


def test_unreadable_browser_history_names_the_place_without_a_button(monkeypatch):
    from service.assistant import sync_status
    from service.tools import browser_history_tools as B
    monkeypatch.setattr(B, "browser_history_sync_state", lambda: "unavailable")
    noted, _ = access_needed.start()
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock, return_value=_readiness()):
        answer = asyncio.run(B.search_browser_history_impl("recipes"))
    assert "Privacy & Security > Full Disk Access" in answer
    # A missing browser looks the same to the reader, so no fix is offered.
    assert noted == []


# --- The Allow button survives a reopened chat ----------------------------------

def test_a_turns_kinds_are_its_tool_results_fixable_notes_in_order():
    results = [
        {"type": "tool_result", "needs_access": [{"source": "calendar", "kind": "calendar_access"}]},
        {"type": "tool_result"},
        {"type": "tool_result", "needs_access": [{"source": "mail", "kind": "mail_closed"},
                                                 {"source": "calendar", "kind": "calendar_access"},
                                                 {"source": "web", "kind": "made_up"}, "junk"]},
    ]
    assert access_needed.kinds(results) == ["calendar_access", "mail_closed"]
    assert access_needed.kinds([]) == []


def test_a_stored_reply_keeps_its_kinds_for_the_chat_window_only(tmp_path):
    from service.memory.store import SessionStore
    store = SessionStore(tmp_path / "chats.db")
    sid = store.create_session()
    store.add_turn(sid, "user", "What's on today?", access_kinds=["calendar_access"])
    store.add_turn(sid, "assistant", "I can't read your calendar yet.",
                   access_kinds=["calendar_access", "made_up", "calendar_access", "full_disk_access"])
    store.add_turn(sid, "assistant", "Nothing blocked here.")
    shown = store.display_turns_from(sid, 0)
    assert "access_kinds" not in shown[0] and "access_kinds" not in shown[2]
    assert shown[1]["access_kinds"] == ["calendar_access", "full_disk_access"]
    # What the model sees again is unchanged: the kinds are for the window only.
    assert all("access_kinds" not in turn for turn in store.turns_from(sid, 0))


def test_an_older_chat_database_gains_the_column_and_keeps_its_turns(tmp_path):
    import sqlite3
    from service.memory.store import SessionStore
    path = tmp_path / "older.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE turns (session_id TEXT, idx INTEGER, role TEXT, content TEXT, "
               "tool_digest TEXT, created_at REAL, display_content TEXT, display_kind TEXT, "
               "PRIMARY KEY (session_id, idx))")
    db.execute("INSERT INTO turns VALUES ('old', 0, 'assistant', 'Earlier reply', NULL, 1, NULL, NULL)")
    db.commit()
    db.close()
    for _ in range(2):
        store = SessionStore(path)
        assert store.display_turns_from("old", 0) == [
            {"session_id": "old", "idx": 0, "role": "assistant", "content": "Earlier reply",
             "tool_digest": None, "created_at": 1.0}]
        assert [r[1] for r in store._db.execute("PRAGMA table_info(turns)")].count("access_kinds") == 1
        store._db.close()
    # A malformed saved value draws no button rather than failing the reload.
    db = sqlite3.connect(path)
    db.execute("UPDATE turns SET access_kinds='not json' WHERE session_id='old'")
    db.commit()
    db.close()
    store = SessionStore(path)
    assert "access_kinds" not in store.display_turns_from("old", 0)[0]
    store._db.close()


def test_a_reopened_chat_shows_the_reply_s_allow_button(monkeypatch, tmp_path):
    """A calendar read that macOS refused, answered over /agent, comes back from
    /sessions/{id} with the kind the live reply's button was drawn from."""
    import json
    import types
    from fastapi.testclient import TestClient
    from service import main
    from service.memory import context
    from service.memory.store import SessionStore
    from service.router import router as R
    from service.safety.policy import Decision, Tier
    from service.tools.registry import REGISTRY
    from service.workflows import reads

    async def calendar_denied(**kwargs):
        return ("Wisp could not check Calendar. " + access_needed.need("calendar", "calendar_access")
                + " Then try again.")

    async def no_engine():
        pass
    store = SessionStore(tmp_path / "agent.db")
    monkeypatch.setitem(REGISTRY, "get_upcoming", replace(REGISTRY["get_upcoming"], func=calendar_denied))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(context, "store", store)
    monkeypatch.setattr(main, "client", types.SimpleNamespace(), raising=False)
    monkeypatch.setattr(main, "ensure_omlx", no_engine)
    config = lambda: {"tool_retrieval": {"provider": "lexical"}}
    monkeypatch.setattr(main, "models_config", config)
    monkeypatch.setattr(R, "models_config", config)
    monkeypatch.setattr(reads, "decide", lambda *_a, **_k: Decision(Tier.ALLOW, "test"))
    http = TestClient(main.app)
    with http.stream("POST", "/agent", json={"prompt": "What's on my calendar today?"}) as response:
        events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
    assert not [e for e in events if e["type"] == "error"], events
    live = [e for e in events if e["type"] == "tool_result" and e.get("needs_access")]
    assert live and live[0]["needs_access"] == [{"source": "calendar", "kind": "calendar_access"}]
    sid = events[0]["id"]
    turns = http.get(f"/sessions/{sid}").json()["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert "access_kinds" not in turns[0]
    assert turns[1]["access_kinds"] == ["calendar_access"]
