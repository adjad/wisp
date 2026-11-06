"""An effect tool that refused before writing frees the chat; anything else locks it.

Real tools, real agent loop and real /agent turns; a fake app bridge, a fake
scheduled-send queue, fake Notes results and a scripted model. Both sides of the
safety line are here: a typed refusal is recorded as not done and the user can
correct it in the same chat, and an unknown outcome still locks, whatever its
text says. Probe 2026-11-04: add_reminder with a bad when_iso, and schedule_send
with an unreadable time, each locked the chat.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from service import main
from service.assistant import outbox
from service.memory.store import SessionStore
from service.router.model_led import family_for, registry_specs
from service.tools import (action_tools, assistant_tools, memory_tools, notes_write, registry,
                           schedule_extras, timers_alarms)
from tests.test_model_led_entrypoint import endpoint, tool_call  # noqa: F401
from tests.test_send_recipient_preflight import chat as send_chat  # noqa: F401

REAL_TOOLS = {name: registry.REGISTRY[name] for name in (
    "add_reminder", "update_reminder", "add_calendar_event", "update_event", "cancel_event",
    "complete_reminder", "schedule_send", "cancel_scheduled_send", "set_timer",
    "create_note", "append_note", "remember")}
NOTICE = main.UNRESOLVED_ACTION_NOTICE
LATER = (dt.datetime.now() + dt.timedelta(days=2)).replace(microsecond=0, second=0)
LATER_ISO = LATER.isoformat(timespec="minutes")
PAST_ISO = (dt.datetime.now() - dt.timedelta(days=2)).isoformat(timespec="minutes")


class Queue:
    """Stand-in for the scheduled-send queue: records every row it is given."""

    def __init__(self):
        self.added, self.fail = [], False

    def add(self, **row):
        if self.fail:
            raise OSError("synthetic queue failure")
        self.added.append(row)
        return "fixture-id"


@pytest.fixture
def chat(send_chat, monkeypatch):  # noqa: F811
    """A real /agent chat with the real effect tools behind a fake bridge."""
    registry.REGISTRY.update(REAL_TOOLS)
    send_chat.written = []
    send_chat.reminder_answer = lambda op, payload: {"ok": True, "status": "succeeded"}
    send_chat.connected = True

    async def request(op, payload, **kwargs):
        send_chat.written.append((op, payload))
        return send_chat.reminder_answer(op, payload)

    async def connected(**kwargs):
        return send_chat.connected
    monkeypatch.setattr(outbox, "request", request)
    monkeypatch.setattr(outbox, "app_connected", connected)
    send_chat.queue = Queue()
    monkeypatch.setattr("service.assistant.outbound_queue.outbound_queue", send_chat.queue)
    return send_chat


def _turn(chat, name, args, prompt, then="Unused."):
    """One turn where the model calls the real tool `name` with `args`."""
    family = family_for(registry_specs({name: REAL_TOOLS[name]})[0])
    chat.script = [[tool_call("get_tool_schemas", families=[family], tools=[name])],
                   [tool_call(name, **args)], then]
    return chat.request(prompt, chat.sid)


def _text(events):
    return "".join(e.get("text", "") for e in events if e["type"] == "text")


def _settled(chat):
    return [tuple(row) for row in chat.store._db.execute(
        "SELECT status, json_extract(state_json,'$.status') FROM workflows "
        "WHERE session_id=? AND kind='model_led_effect'", (chat.sid,)).fetchall()]


def _assert_locked_next_turn(chat, name, args):
    assert chat.store.unresolved_action(chat.sid) is not None
    written = len(chat.written)
    chat.script = [[tool_call(name, **args)], "Okay."]
    assert _text(chat.request("try that again", chat.sid)).startswith(NOTICE)
    assert len(chat.written) == written


# ---- A definite refusal frees the chat ----

def test_add_reminder_with_a_bad_date_asks_and_the_user_corrects_it_in_the_same_chat(chat):
    bad = {"title": "Call the dentist", "when_iso": "next blursday"}
    text = _text(_turn(chat, "add_reminder", bad, "remind me to call the dentist next blursday"))
    assert text == ("I couldn't read that time, so I didn't add the reminder. "
                    "What date and time should I use?")
    assert chat.written == []  # nothing reached the app
    assert chat.store.unresolved_action(chat.sid) is None
    assert chat.store.pending_model_effects(chat.sid) == []
    assert _settled(chat) == [("completed", "not_done")]

    good = {"title": "Call the dentist", "when_iso": LATER_ISO}
    chat.script = [[tool_call("add_reminder", **good)], "Set."]
    events = chat.request(f"sorry, {LATER_ISO}", chat.sid)
    assert NOTICE not in _text(events)
    assert [op for op, _ in chat.written] == ["create_reminder"]
    assert chat.store.unresolved_action(chat.sid) is None


def test_schedule_send_with_an_unreadable_time_asks_and_the_user_corrects_it_in_the_same_chat(chat):
    call = {"channel": "email", "to": "sam.fixture@example.com", "body": "Thursday works."}
    text = _text(_turn(chat, "schedule_send", {**call, "when": "whenever the moon is out at 99:99"},
                       "email sam tomorrow at 9am"))
    assert text == ("I haven't sent anything. I couldn't read that time, so nothing is scheduled. "
                    "What time should I send it?")
    assert chat.queue.added == []
    assert chat.store.unresolved_action(chat.sid) is None
    assert _settled(chat) == [("completed", "not_sent")]

    chat.script = [[tool_call("schedule_send", **call, when="tomorrow at 9am")], "Scheduled."]
    assert NOTICE not in _text(chat.request("tomorrow at 9am", chat.sid))
    assert [row["recipient"] for row in chat.queue.added] == ["sam.fixture@example.com"]
    assert chat.store.unresolved_action(chat.sid) is None


def test_a_refusal_keeps_the_earlier_receipt_in_the_same_turn(chat):
    family = family_for(registry_specs({"add_reminder": REAL_TOOLS["add_reminder"]})[0])
    chat.script = [[tool_call("get_tool_schemas", families=[family], tools=["add_reminder"])],
                   [tool_call("add_reminder", title="First", when_iso=LATER_ISO)],
                   [tool_call("add_reminder", title="Second", when_iso="not a date")], "Unused."]
    text = _text(chat.request("two reminders", chat.sid))
    assert text.startswith("Reminder set: “First”") and "didn't add the reminder" in text
    assert chat.store.unresolved_action(chat.sid) is None


@pytest.mark.parametrize("name, args, prompt, shown", [
    ("set_timer", {"duration": "soonish"}, "set a timer please",
     "I couldn't read that as a length of time, so I didn't set a timer. How long should it run?"),
    ("append_note", {"title": "shopping", "text": "milk"}, "add milk to my shopping note",
     "I couldn't find a note called “shopping”, so nothing was added. Should I start a new one?"),
])
def test_tools_outside_the_effect_table_stop_with_a_person_facing_question(chat, monkeypatch, name, args, prompt, shown):
    monkeypatch.setattr(notes_write, "_osa", lambda script: subprocess.CompletedProcess(
        [], 0, stdout="NONE\n", stderr=""))
    assert _text(_turn(chat, name, args, prompt)) == shown
    assert chat.store.unresolved_action(chat.sid) is None
    assert _settled(chat) == [("completed", "not_done")]


def _row(title="Water plants", **extra):
    return {"id": title, "title": title, "when_ts": LATER.timestamp(), "source": "manual",
            "created_at": 1.0, "updated_at": 1.0, **extra}


def _no_write_store(monkeypatch, *rows):
    """Reads answer with `rows`; any write fails the test."""
    def write(*args, **kwargs):
        raise AssertionError("a refusal must not write")
    store = SimpleNamespace(upcoming=lambda **kw: list(rows), history=lambda **kw: [], get=lambda cid: None,
                            active_future=lambda **kw: list(rows), add_manual=write, update_schedule=write)
    monkeypatch.setattr(assistant_tools, "assistant_store", store)
    monkeypatch.setattr(assistant_tools, "reminders_matching", lambda *args, **kwargs: list(rows))
    monkeypatch.setattr(schedule_extras, "_store", store)
    monkeypatch.setattr(schedule_extras, "complete_commitment", write)
    monkeypatch.setattr(assistant_tools, "_retire", write)


def _no_bridge(monkeypatch):
    async def bridge(*args, **kwargs):
        raise AssertionError("a refusal must not reach the app")

    async def connected(**kwargs):
        return True
    monkeypatch.setattr(outbox, "request", bridge)
    monkeypatch.setattr(outbox, "app_connected", connected)


def _osa_says(monkeypatch, output):
    calls = []

    def osa(script):
        calls.append(script)
        return subprocess.CompletedProcess([], 0, stdout=output, stderr="")
    monkeypatch.setattr(notes_write, "_osa", osa)
    return calls


TWO = (_row("Water plants"), _row("Water garden"))


@pytest.mark.parametrize("name, args, rows", [
    ("add_reminder", {"title": "x", "when_iso": "not a date"}, ()),
    ("add_reminder", {"title": "x", "when_iso": PAST_ISO}, ()),
    ("add_reminder", {"title": "  ", "when_iso": LATER_ISO}, ()),
    ("add_reminder", {"title": "x", "when_iso": "not a date", "all_day": True}, ()),
    ("add_calendar_event", {"title": "x", "when_iso": "not a date"}, ()),
    ("add_calendar_event", {"title": "x", "when_iso": PAST_ISO}, ()),
    ("add_calendar_event", {"title": " ", "when_iso": LATER_ISO}, ()),
    ("update_reminder", {"title": "water"}, TWO),
    ("update_reminder", {"title": "water", "when_iso": LATER_ISO}, TWO),
    ("update_reminder", {"title": "Water plants", "when_iso": "not a date"}, (_row(),)),
    ("update_reminder", {"title": "Water plants", "when_iso": PAST_ISO}, (_row(),)),
    ("cancel_event", {"title": "water"}, TWO),
    ("complete_reminder", {"title": "water"}, TWO),
    ("complete_reminder", {"title": "   "}, ()),
    ("set_timer", {"duration": "soonish"}, ()),
    ("set_timer", {"duration": "0"}, ()),
    ("set_timer", {"duration": "400 hours"}, ()),
    ("set_timer", {"kind": "alarm", "time": "half past nothing"}, ()),
    ("set_timer", {"kind": "sleep", "duration": "soonish"}, ()),
    ("cancel_scheduled_send", {"id": "  "}, ()),
])
def test_each_marked_refusal_is_proof_and_writes_nothing(monkeypatch, name, args, rows):
    _no_write_store(monkeypatch, *rows)
    _no_bridge(monkeypatch)
    before = dict(timers_alarms._TIMERS)
    tool = REAL_TOOLS[name]
    result = asyncio.run(registry.run_tool(tool, args))
    assert registry.not_done_receipt(tool, result), result
    assert registry.classify_tool_outcome(name, result).status in {"failed", "needs_input"}
    assert timers_alarms._TIMERS == before


@pytest.mark.parametrize("call, output", [
    ({"name": "append_note", "title": "shopping", "text": "milk"}, "NONE\n"),
    ({"name": "append_note", "title": "shopping", "text": "milk"}, "MANY:Shopping\nShopping old\n"),
    ({"name": "create_note", "title": "Packing", "folder": "Nope"}, "NOFOLDER\n"),
])
def test_notes_refusals_are_proof_only_on_the_scripts_early_return(monkeypatch, call, output):
    _osa_says(monkeypatch, output)
    tool = REAL_TOOLS[call["name"]]
    args = {k: v for k, v in call.items() if k != "name"}
    assert registry.not_done_receipt(tool, asyncio.run(registry.run_tool(tool, args)))


@pytest.mark.parametrize("call, output, code, stderr", [
    ({"name": "append_note", "title": "shopping", "text": "milk"}, "OK:Shopping\n", 0, ""),
    ({"name": "append_note", "title": "shopping", "text": "milk"}, "", 1, "synthetic Notes failure"),
    ({"name": "create_note", "title": "Packing"}, "", 1, "synthetic Notes failure"),
])
def test_notes_other_outcomes_are_not_proof(monkeypatch, call, output, code, stderr):
    monkeypatch.setattr(notes_write, "_osa", lambda script: subprocess.CompletedProcess(
        [], code, stdout=output, stderr=stderr))
    tool = REAL_TOOLS[call["name"]]
    args = {k: v for k, v in call.items() if k != "name"}
    assert not registry.not_done_receipt(tool, asyncio.run(registry.run_tool(tool, args)))


@pytest.mark.parametrize("fact, quote", [
    ("my sister is Ana", "what's the weather like"),              # never asked to save
    ("my sister is Anna", "remember that my sister is Ana"),      # paraphrased
])
def test_remember_refusals_are_proof(monkeypatch, fact, quote):
    monkeypatch.setattr(memory_tools, "current_source", SimpleNamespace(
        get=lambda: {"quote": quote, "session_id": "s"}))
    tool = REAL_TOOLS["remember"]
    assert registry.not_done_receipt(tool, asyncio.run(registry.run_tool(tool, {"fact": fact})))


def test_cancelling_a_send_no_longer_pending_is_proof_it_changed_nothing(monkeypatch):
    row = {"status": "sent"}
    monkeypatch.setattr("service.assistant.outbound_queue.outbound_queue", SimpleNamespace(
        cancel=lambda sid: None, get=lambda sid: row))
    monkeypatch.setattr(action_tools, "_scheduled_send_record", lambda row, full_body: "[record]")
    tool = REAL_TOOLS["cancel_scheduled_send"]
    result = asyncio.run(registry.run_tool(tool, {"id": "abc"}))
    assert registry.not_done_receipt(tool, result) and "NOT cancelled" in result


def test_a_move_that_changed_after_the_card_is_proof_it_changed_nothing(monkeypatch):
    _no_bridge(monkeypatch)
    binding = {"title": "Standup", "recurring": False,
               "payload": {"source_id": "s", "when_ts": LATER.timestamp(), "expected_start_ts": 1.0,
                           "expected_title": "Standup", "invitee_count": 0}}
    monkeypatch.setattr(assistant_tools, "assistant_store", SimpleNamespace(
        calendar_occurrence_current=lambda *args: False))
    tool = REAL_TOOLS["update_event"]
    with assistant_tools.bound_event_move(binding):
        result = asyncio.run(registry.run_tool(tool, {"title": "Standup", "when_iso": LATER_ISO}))
    assert registry.not_done_receipt(tool, result) and "Nothing was changed" in result


# ---- An unknown outcome still locks ----

@pytest.mark.parametrize("answer", [
    {"ok": False, "status": "unknown", "error": "the app stopped answering"},
    {"ok": False, "error": "no status at all"},
])
def test_a_reminder_with_an_unconfirmed_app_answer_locks_the_chat(chat, answer):
    args = {"title": "Call the dentist", "when_iso": LATER_ISO}
    chat.reminder_answer = lambda op, payload: answer
    text = _text(_turn(chat, "add_reminder", args, "remind me to call the dentist"))
    assert "did not return a verified success" in text
    assert len(chat.written) == 1
    assert _settled(chat) == [("uncertain", "uncertain")]
    _assert_locked_next_turn(chat, "add_reminder", {**args, "title": "Something else"})


def test_a_reminder_whose_bridge_raises_locks_the_chat(chat):
    def boom(op, payload):
        raise TimeoutError("synthetic bridge timeout")
    chat.reminder_answer = boom
    args = {"title": "Call the dentist", "when_iso": LATER_ISO}
    _turn(chat, "add_reminder", args, "remind me to call the dentist")
    _assert_locked_next_turn(chat, "add_reminder", {**args, "title": "Something else"})


def test_an_earlier_unconfirmed_native_write_keeps_the_lock(chat, monkeypatch):
    """Nothing was written this time, but an earlier attempt may exist: not proof."""
    chat.connected = False
    monkeypatch.setattr(outbox, "reminder_create_fallback_allowed", lambda payload: False)
    args = {"title": "Call the dentist", "when_iso": LATER_ISO}
    _turn(chat, "add_reminder", args, "remind me to call the dentist")
    assert chat.written == []
    assert chat.store.unresolved_action(chat.sid) is not None


def test_a_queue_failure_on_schedule_send_locks_the_chat(chat):
    chat.queue.fail = True
    call = {"channel": "email", "to": "sam.fixture@example.com", "body": "Thursday works.",
            "when": "tomorrow at 9am"}
    _turn(chat, "schedule_send", call, "email sam tomorrow at 9")
    assert chat.store.unresolved_action(chat.sid) is not None


def test_a_refusal_shaped_string_from_the_tool_locks_the_chat(chat):
    """The exact words of a refusal are not proof: only the type is."""
    async def plain(title, when_iso, **kwargs):
        return f"(bad when_iso {when_iso!r} — nothing was added.)"
    registry.REGISTRY["add_reminder"] = replace(REAL_TOOLS["add_reminder"], func=plain)
    family = family_for(registry_specs({"add_reminder": REAL_TOOLS["add_reminder"]})[0])
    chat.script = [[tool_call("get_tool_schemas", families=[family], tools=["add_reminder"])],
                   [tool_call("add_reminder", title="x", when_iso="not a date")], "Unused."]
    chat.request("remind me", chat.sid)
    assert _settled(chat) == [("uncertain", "uncertain")]


def test_only_the_typed_result_from_the_real_tool_is_proof():
    tool = REAL_TOOLS["add_reminder"]
    proof = registry.not_done("(not a date; nothing was added.)", question="When?")
    assert registry.not_done_receipt(tool, proof)
    assert copy.deepcopy(proof) is proof and proof.question == "When?"
    with pytest.raises(AttributeError):
        proof.question = "other"
    assert not registry.not_done_receipt(tool, str(proof))  # plain text is no proof
    assert not registry.not_done_receipt(tool, proof[:10])  # a slice is plain text
    assert not registry.not_done_receipt(tool, "(not a date; nothing was added.)")

    async def elsewhere(**args):
        return proof
    assert not registry.not_done_receipt(replace(tool, func=elsewhere), proof)  # an extension's look-alike
    # A tool outside the list keeps locking, even with a proof in hand.
    assert not registry.not_done_receipt(registry.Tool("run_shell", "fixture", {}, "shell", tool.func), proof)
    assert not registry.not_done_receipt(registry.Tool("write_file", "fixture", {}, "fs_write", tool.func), proof)
    # The two proofs don't stand in for each other.
    assert not registry.not_done_receipt(REAL_TOOLS["schedule_send"], action_tools._not_sent("(x)"))
    assert not registry.not_sent_receipt(REAL_TOOLS["schedule_send"], proof)
    # Unknown-outcome text is neither.
    unknown = registry._OutboundUnknownResult("Outcome unknown.", operation="send_email", action_id="a")
    assert not registry.not_done_receipt(tool, unknown)


def test_every_listed_tool_comes_from_its_named_module():
    for name, module in registry._NOT_DONE_TOOLS.items():
        assert REAL_TOOLS[name].func.__module__ == module, name


def test_a_marked_refusal_never_reads_as_success():
    result = registry.not_done("Reminder set: not really")
    assert registry.classify_tool_outcome("add_reminder", result).status == "failed"
    assert registry.classify_tool_outcome("add_reminder", result, denied=True).status == "denied"
    moved = registry.not_done("Moved “Standup” to Fri Nov 6, 9:00 AM–9:30 AM.")
    assert registry.classify_tool_outcome("update_event", moved).status == "failed"
    empty = registry.not_done("Nothing active matches that.")
    assert registry.classify_tool_outcome("cancel_event", empty).status == "failed"
    asks = registry.not_done("Several match — which one?")
    assert registry.classify_tool_outcome("cancel_event", asks).status == "needs_input"


def test_store_settles_not_done_apart_from_a_completed_action(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    try:
        sid = store.create_session()
        args = {"title": "x", "when_iso": "not a date"}
        claim = store.claim_model_effect(sid, "one", "add_reminder", args)
        assert store.finish_model_effect(sid, claim, verified=False, not_done=True)
        assert store.unresolved_action(sid) is None and store.pending_model_effects(sid) == []
        row = store._db.execute("SELECT status, json_extract(state_json,'$.status') FROM workflows").fetchone()
        assert tuple(row) == ("completed", "not_done")
        # Settling again is refused, and the same request can't claim it twice.
        assert not store.finish_model_effect(sid, claim, verified=False)
        assert not store.claim_model_effect(sid, "one", "add_reminder", args)["admitted"]
        # A later request may try the same call, and an unverified one still blocks.
        retry = store.claim_model_effect(sid, "two", "add_reminder", args)
        assert retry["admitted"]
        assert store.finish_model_effect(sid, retry, verified=False)
        assert store.unresolved_action(sid) is not None
    finally:
        store._db.close()


def test_every_finish_callback_accepts_the_refusal_keywords():
    """A refused call on a path whose callback can't record it would crash the turn."""
    import inspect
    import scripts.toolbench.runner as runner
    assert "**refusal" in inspect.getsource(runner)
    assert "**refusal" in inspect.getsource(main)
