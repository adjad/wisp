"""One request, several writes: the loop refuses only a TRUE repeat of an effect.

"Make these into reminders" with three items, "add these two events", or
"turn on dark mode and Do Not Disturb" are distinct writes and all run. The
same write twice (same tool, equivalent arguments or the same target) is
refused, and a per-turn cap bounds a runaway. Picture turns still card every
write.

Synthetic tools and a scripted client only: nothing loads a model, and nothing
is written, sent or changed.
"""
from __future__ import annotations

import json

import pytest

from service.agent import loop
from tests.test_model_led_integration import (  # noqa: F401  (pytest fixture)
    Approval, ScriptedClient, synthetic)
from tests.test_vision_actions import run_picture
import asyncio
import copy


def call(cid, tool_name, **args):
    return {"id": cid, "function": {"name": tool_name, "arguments": json.dumps(args)}}


REMINDER = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
SETTING = {"type": "object", "properties": {"setting": {"type": "string"}, "on": {"type": "boolean"}},
           "required": ["setting", "on"]}


@pytest.fixture
def writes(synthetic, monkeypatch):
    effects, register = synthetic
    monkeypatch.setattr("service.safety.policy._FULL_ACCESS", True)
    reminder = register("add_reminder", "assistant_tools", "assistant_write", parameters=REMINDER)
    setting = register("toggle_setting", "system_tools", "system_write", parameters=SETTING)

    async def add_reminder(**args):
        effects.append(("add_reminder", copy.deepcopy(args)))
        return f"Reminder set: {args['title']}"

    async def toggle_setting(**args):
        effects.append(("toggle_setting", copy.deepcopy(args)))
        return f"{args['setting']} is now {'on' if args['on'] else 'off'}."
    for tool, fn in ((reminder, add_reminder), (setting, toggle_setting)):
        fn.__module__ = tool.func.__module__
        monkeypatch.setattr(tool, "func", fn)
    return effects


def run(script, tools, prompt="make these into reminders", approval=None):
    events = []
    client = ScriptedClient(script)

    async def emit(event):
        events.append(copy.deepcopy(event))
    answer = asyncio.run(loop.run_agent(
        client, "synthetic-agent", [{"role": "user", "content": prompt}], emit,
        approval or Approval(True), tools=list(tools), multi_round=True,
        include_memory_context=False))
    return events, answer


def refusals(events):
    return [e["result"] for e in events if e["type"] == "tool_result"
            and "NOT run" in str(e.get("result"))]


def test_distinct_writes_in_one_step_all_run(writes):
    events, _ = run([[call("a", "add_reminder", title="Buy milk"),
                      call("b", "add_reminder", title="Call the dentist"),
                      call("c", "add_reminder", title="Pay rent")], "Added all three."],
                    ["add_reminder"])
    assert [a["title"] for n, a in writes] == ["Buy milk", "Call the dentist", "Pay rent"]
    assert not refusals(events)


def test_distinct_writes_across_steps_and_two_settings_run(writes):
    events, _ = run([[call("a", "toggle_setting", setting="dark_mode", on=True)],
                     [call("b", "toggle_setting", setting="do_not_disturb", on=True)],
                     "Both are on."], ["toggle_setting"], prompt="turn on dark mode and DND")
    assert [a["setting"] for n, a in writes] == ["dark_mode", "do_not_disturb"]
    assert not refusals(events)


def test_identical_repeat_is_refused(writes):
    events, _ = run([[call("a", "add_reminder", title="Buy milk")],
                     [call("b", "add_reminder", title="  buy   MILK ")],
                     "Added it."], ["add_reminder"])
    assert [a["title"] for n, a in writes] == ["Buy milk"]
    assert any("already succeeded earlier this turn" in r for r in refusals(events))


def test_same_target_with_other_arguments_is_a_repeat(writes):
    # Flip-flopping one setting in one turn is a loop, not a request.
    events, _ = run([[call("a", "toggle_setting", setting="dark_mode", on=True)],
                     [call("b", "toggle_setting", setting="dark_mode", on=False)],
                     "Done."], ["toggle_setting"], prompt="turn on dark mode")
    assert writes == [("toggle_setting", {"setting": "dark_mode", "on": True})]
    assert any("already succeeded" in r for r in refusals(events))


def test_per_turn_cap_bounds_a_runaway(writes):
    cap = loop._MAX_EFFECT_CALLS_PER_TURN
    batch = [call(f"r{i}", "add_reminder", title=f"Item {i}") for i in range(cap + 3)]
    events, _ = run([batch, "Done."], ["add_reminder"])
    assert len(writes) == cap
    capped = [r for r in refusals(events) if "the most one request may make" in r]
    assert len(capped) == 3


def test_picture_turn_multi_reminder_cards_every_write(writes):
    approval = Approval(True)
    reading = "TEXT:\nBuy milk\nCall the dentist\nPay rent\nDESCRIPTION:\nA handwritten list."
    _, events, answer = run_picture(
        [[call("a", "add_reminder", title="Buy milk"),
          call("b", "add_reminder", title="Call the dentist"),
          call("c", "add_reminder", title="Pay rent")], "Added all three."],
        reading=reading, prompt="make these into reminders", tools=["add_reminder"],
        approval=approval)
    decisions = [e["decision"] for e in events if e["type"] == "tool_call"
                 and e["name"] == "add_reminder"]
    assert decisions == ["confirm"] * 3                       # full access, still carded
    assert [c["tool"] for c in approval.calls] == ["add_reminder"] * 3
    assert [a["title"] for n, a in writes] == ["Buy milk", "Call the dentist", "Pay rent"]


def test_duplicate_key_ignores_concurrency_tokens_and_keys_sends_by_target():
    key = loop._effect_duplicate_key
    assert (key("delete_knowledge", {"record_id": "k1", "expected_revision": "a"})
            == key("delete_knowledge", {"record_id": "k1", "expected_revision": "b"}))
    assert (key("send_email", {"to": "sam@example.com", "body": "Hi"})
            == key("send_email", {"to": "Sam@Example.com", "body": "Hello again"}))
    assert (key("send_email", {"to": "sam@example.com", "body": "Hi"})
            != key("send_email", {"to": "alex@example.com", "body": "Hi"}))
    assert (key("add_calendar_event", {"title": "Lunch", "when_iso": "2026-11-02T12:00"})
            != key("add_calendar_event", {"title": "Dinner", "when_iso": "2026-11-02T19:00"}))
