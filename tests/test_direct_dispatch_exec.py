"""run_agent's execution of router-resolved `direct_calls`.

The router half (which requests qualify) is tested in test_direct_dispatch.py.
This is the other half: that a pre-resolved call goes through the SAME
decide() -> approver -> run_tool() -> audit() path a model-driven call does, and
leaves the loop in the same state.

What must keep holding:
  * the call runs BEFORE the first model call, and the model's first step is
    already a narration over the result;
  * a single pre-synthesized summary call ends the turn with ZERO model calls —
    the whole point of the summary route (one model call total: the
    summarizer's own internal synthesis);
  * a CONFIRM-tier tool (lock_screen is system_write, run_speed_test is
    network_active) still raises the card — a direct call is not a way around
    it — and a DENIAL is recorded so the narration gate can't fire on a call
    that never ran;
  * the transcript stays well-formed: a `tool` message must follow an assistant
    message carrying the matching tool_call_id, or the chat template sees a
    malformed conversation;
  * test mode executes nothing.

    .venv/bin/python tests/test_direct_dispatch_exec.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402
from service.tools import registry  # noqa: E402
from service.tools.registry import REGISTRY, Tool  # noqa: E402

PASS, FAIL = 0, 0
SUMMARY_TEXT = "Your inbox, already written as warm prose by the summarizer."
DEVICE_TEXT = "battery: 84%, not charging"

ran: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def _register_fakes() -> None:
    """`system_read` is ALLOW; `network_active` is in policy._ALWAYS_CONFIRM.

    network_active, NOT system_write, is what the confirm-tier fake uses — the
    shipped policy.yaml sets `full_access: true`, which auto-allows system_write
    (so real lock_screen does NOT raise a card on this machine). _ALWAYS_CONFIRM
    is the tier that survives full_access, and run_speed_test — a genuine
    direct-dispatch target — is exactly that category. Testing the tier the user
    actually runs under is the point; a system_write fake would silently assert
    nothing.
    """
    def mk(name, out, category):
        def fn(**kwargs):
            ran.append(name)
            return out
        REGISTRY[name] = Tool(name=name, description=f"fake {name}",
                              parameters={"type": "object", "properties": {}},
                              category=category, func=fn)
    mk("fake_summary", SUMMARY_TEXT, "system_read")
    mk("fake_device", DEVICE_TEXT, "system_read")
    mk("fake_confirmed", "speed test: 480 Mbps down", "network_active")


@pytest.fixture(autouse=True)
def _registered_fake_tools():
    names = ("fake_summary", "fake_device", "fake_confirmed")
    previous = {name: REGISTRY.get(name) for name in names}
    _register_fakes()
    yield
    for name, tool in previous.items():
        if tool is None:
            REGISTRY.pop(name, None)
        else:
            REGISTRY[name] = tool


class ScriptedClient:
    """Records every model call it is asked to make."""

    def __init__(self, script: list[dict] | None = None) -> None:
        self.calls: list[list[dict]] = []
        self._script = iter(script or [{"content": "NARRATED"}])

    async def ensure_only(self, model, *, exclusive=False, emit=None, **kw):
        return None

    async def stream_events(self, model, messages, *, tools=None, tool_choice=None,
                            temperature=None, max_tokens=2048, **extra):
        self.calls.append(list(messages))
        step = next(self._script, {"content": "NARRATED"})
        content = step.get("content", "")
        if content:
            yield {"kind": "content", "text": content}
        yield {"kind": "final", "message": {"role": "assistant", "content": content,
                                                 "tool_calls": step.get("tool_calls")}}


class Approver:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.seen: list[dict] = []

    async def confirm(self, action):
        self.seen.append(action)
        return self.answer


def run(direct, *, approver=None, short_circuit=None, test_mode=False, tools=None,
        messages=None, client=None, **contract):
    client = client or ScriptedClient()
    approver = approver or Approver()
    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    out = asyncio.run(loop.run_agent(
        client, "Agents-A1-4B-oQe6", messages or [{"role": "user", "content": "go"}],
        emit, approver,
        tools=tools if tools is not None else [n for n, _ in direct],
        max_steps=3, direct_calls=direct,
        short_circuit_tools=short_circuit, test_mode=test_mode, **contract))
    return out, client, approver, events


def _tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def test_strict_private_all_no_match_skips_narration(monkeypatch) -> None:
    monkeypatch.setitem(REGISTRY, "view_emails", Tool(
        "view_emails", "synthetic mail", {"type": "object", "properties": {}},
        "assistant_read", lambda: "No emails matching 'synthetic orientation'."))
    monkeypatch.setitem(REGISTRY, "get_upcoming", Tool(
        "get_upcoming", "synthetic calendar", {"type": "object", "properties": {}},
        "assistant_read", lambda: "Today is Monday. Nothing scheduled in this week."))
    out, client, _, _ = run(
        [("view_emails", {}), ("get_upcoming", {})],
        tools=["view_emails", "get_upcoming"], multi_round=True,
        required_tool_groups=(frozenset({"view_emails", "get_upcoming"}),))
    assert out == "No emails matching 'synthetic orientation'."
    assert client.calls == []


def test_strict_private_partial_success_narrates_verified_results_only(monkeypatch) -> None:
    monkeypatch.setitem(REGISTRY, "view_emails", Tool(
        "view_emails", "synthetic mail", {"type": "object", "properties": {}},
        "assistant_read", lambda: "No emails matching 'synthetic orientation'."))
    monkeypatch.setitem(REGISTRY, "get_upcoming", Tool(
        "get_upcoming", "synthetic calendar", {"type": "object", "properties": {}},
        "assistant_read", lambda: "TUESDAY 10:00 AM | Synthetic orientation"))
    messages = [{"role": "user", "content": "synthetic orientation request"}]
    out, client, _, _ = run(
        [("view_emails", {}), ("get_upcoming", {})], messages=messages,
        tools=["view_emails", "get_upcoming"], multi_round=True,
        narration_after=frozenset({"view_emails", "get_upcoming"}),
        include_memory_context=False,
        required_tool_groups=(frozenset({"view_emails", "get_upcoming"}),))
    assert out == "NARRATED"
    assert len(client.calls) == 1
    assert "unrelated private stale memory" not in str(client.calls[0])


def test_strict_private_budget_blocks_repeated_widened_email_read(monkeypatch) -> None:
    dispatched: list[dict] = []
    exact = {"query": "financial", "count": 10, "strict_match": True}
    monkeypatch.delitem(registry.UNAVAILABLE_TOOL_REASONS, "view_emails", raising=False)
    monkeypatch.setitem(REGISTRY, "view_emails", Tool(
        "view_emails", "synthetic mail", {"type": "object", "properties": {
            "query": {"type": "string"}, "count": {"type": "integer"},
            "strict_match": {"type": "boolean"}}},
        "assistant_read", lambda **args: dispatched.append(args) or "Verified financial mail."))
    client = ScriptedClient([
        {"tool_calls": [_tool_call("view_emails", {"query": "everything", "count": 100}, "retry-1")]},
        {"content": "NARRATED"},
    ])

    out, _, _, events = run(
        [("view_emails", exact)], client=client, tools=["view_emails"],
        required_tool_groups=(frozenset({"view_emails"}),),
        tool_argument_bindings={"view_emails": exact}, strict_read_limits={"view_emails": 1})

    assert out == "NARRATED"
    assert dispatched == [exact]
    rejected = [event for event in events if event["type"] == "tool_result"
                and "strict read budget" in event.get("result", "")]
    assert len(rejected) == 1
    assert "everything" not in rejected[0]["result"]


def test_strict_private_budget_blocks_repeated_two_source_reads(monkeypatch) -> None:
    dispatched: list[tuple[str, dict]] = []
    email = {"query": "ucsc orientation", "count": 10, "strict_match": True}
    calendar = {"period": "this week", "calendar_only": True, "query": "ucsc orientation"}
    monkeypatch.delitem(registry.UNAVAILABLE_TOOL_REASONS, "view_emails", raising=False)
    monkeypatch.delitem(registry.UNAVAILABLE_TOOL_REASONS, "get_upcoming", raising=False)
    monkeypatch.setitem(REGISTRY, "view_emails", Tool(
        "view_emails", "synthetic mail", {"type": "object", "properties": {
            "query": {"type": "string"}, "count": {"type": "integer"},
            "strict_match": {"type": "boolean"}}},
        "assistant_read", lambda **args: dispatched.append(("view_emails", args)) or "Verified mail."))
    monkeypatch.setitem(REGISTRY, "get_upcoming", Tool(
        "get_upcoming", "synthetic calendar", {"type": "object", "properties": {
            "period": {"type": "string"}, "calendar_only": {"type": "boolean"},
            "query": {"type": "string"}}},
        "assistant_read", lambda **args: dispatched.append(("get_upcoming", args)) or "Verified calendar."))
    client = ScriptedClient([
        {"tool_calls": [
            _tool_call("view_emails", email, "retry-email"),
            _tool_call("get_upcoming", {"period": "60 days", "calendar_only": False}, "retry-calendar"),
        ]},
        {"content": "NARRATED"},
    ])

    out, _, _, events = run(
        [("view_emails", email), ("get_upcoming", calendar)], client=client,
        tools=["view_emails", "get_upcoming"], multi_round=True,
        narration_after=frozenset({"view_emails", "get_upcoming"}),
        required_tool_groups=(frozenset({"view_emails", "get_upcoming"}),),
        tool_argument_bindings={"view_emails": email, "get_upcoming": calendar},
        strict_read_limits={"view_emails": 1, "get_upcoming": 1})

    assert out == "NARRATED"
    assert dispatched == [("view_emails", email), ("get_upcoming", calendar)]
    rejected = [event for event in events if event["type"] == "tool_result"
                and "strict read budget" in event.get("result", "")]
    assert len(rejected) == 2


def test_generic_direct_read_remains_iterative_without_strict_budget() -> None:
    ran.clear()
    client = ScriptedClient([
        {"tool_calls": [_tool_call("fake_device", {}, "repeat-generic")]},
        {"content": "NARRATED"},
    ])

    out, _, _, _ = run([("fake_device", {})], client=client, tools=["fake_device"])

    assert out == "NARRATED"
    assert ran == ["fake_device", "fake_device"]


# --------------------------------------------------------------------------

def test_call_runs_before_the_first_model_call() -> None:
    print("\nthe direct call runs first; the model's first step is already narration")
    ran.clear()
    out, client, _, events = run([("fake_device", {})])
    check("the tool actually ran", ran == ["fake_device"], f"ran={ran}")
    check("exactly one model call (the narration)", len(client.calls) == 1,
          f"{len(client.calls)} calls")
    convo = client.calls[0]
    check("the result was already in the model's context",
          any(m.get("role") == "tool" and DEVICE_TEXT in str(m.get("content"))
              for m in convo))
    check("the answer is the model's narration", out == "NARRATED", f"-> {out!r}")
    kinds = [e["type"] for e in events]
    check("a tool_call event was emitted", "tool_call" in kinds)
    check("a tool_result event was emitted", "tool_result" in kinds)


def test_transcript_stays_well_formed() -> None:
    print("\na tool message must follow an assistant message with the same id")
    ran.clear()
    _, client, _, _ = run([("fake_device", {})])
    convo = client.calls[0]
    tool_idx = next(i for i, m in enumerate(convo) if m.get("role") == "tool")
    prev = convo[tool_idx - 1]
    check("the tool message is preceded by an assistant turn",
          prev.get("role") == "assistant", f"got {prev.get('role')!r}")
    check("that assistant turn carries tool_calls", bool(prev.get("tool_calls")))
    check("the ids match",
          prev["tool_calls"][0]["id"] == convo[tool_idx]["tool_call_id"])
    check("the synthesized call names the right tool",
          prev["tool_calls"][0]["function"]["name"] == "fake_device")


def test_summary_short_circuits_with_zero_model_calls() -> None:
    print("\na pre-synthesized summary ends the turn with NO model call at all")
    ran.clear()
    out, client, _, _ = run([("fake_summary", {})], short_circuit={"fake_summary"})
    check("returns the summarizer's own prose", out == SUMMARY_TEXT, f"-> {out!r}")
    check("the model was never called", client.calls == [],
          f"{len(client.calls)} calls")


def test_confirm_tier_still_raises_the_card() -> None:
    print("\na CONFIRM-tier direct call is not a way around the confirmation card")
    ran.clear()
    approver = Approver(answer=True)
    _, _, approver, _ = run([("fake_confirmed", {})], approver=approver)
    check("the card was raised", len(approver.seen) == 1, f"{approver.seen}")
    check("it named the right tool",
          approver.seen and approver.seen[0]["tool"] == "fake_confirmed")
    check("the tool ran after approval", ran == ["fake_confirmed"], f"ran={ran}")


def test_direct_bulk_reminder_card_has_exact_preview() -> None:
    print("\na router-direct reminder clear previews the exact scoped items")
    from service.tools import assistant_tools
    real = assistant_tools.reminders_matching
    assistant_tools.reminders_matching = lambda scope, query="": [{
        "when_ts": 1787619600.0, "title": "Finish canvas assignment"}]
    try:
        approver = Approver(answer=False)
        run([("clear_reminders", {"scope": "today"})], approver=approver,
            tools=["clear_reminders"])
    finally:
        assistant_tools.reminders_matching = real
    action = approver.seen[0] if approver.seen else {}
    check("the card was raised", len(approver.seen) == 1, str(approver.seen))
    check("the exact reminder is listed",
          "Finish canvas assignment" in action.get("preview", ""), str(action))
    check("the resolved scope is named", "scope 'today'" in action.get("reason", ""),
          str(action))


def test_denied_confirm_does_not_run_and_keeps_thinking_on() -> None:
    print("\na denied card must not run the tool, and must not look like success")
    ran.clear()
    out, client, approver, _ = run([("fake_confirmed", {})],
                                   approver=Approver(answer=False))
    check("the tool did NOT run", ran == [], f"ran={ran}")
    check("the turn still completes via the model", out == "NARRATED", f"-> {out!r}")
    # A denial is a policy outcome (hard_failed), so the narration gate must not
    # fire — the step must still be allowed to think about what to do instead.
    convo = client.calls[0]
    check("the denial is visible to the model",
          any(m.get("role") == "tool" and "denied" in str(m.get("content")).lower()
              for m in convo))


def test_test_mode_executes_nothing() -> None:
    print("\ntest mode reports the plan and runs nothing")
    ran.clear()
    _, _, approver, events = run([("fake_confirmed", {})], test_mode=True)
    check("the tool did NOT run", ran == [], f"ran={ran}")
    check("no confirmation card was raised", approver.seen == [], f"{approver.seen}")
    call_ev = next((e for e in events if e["type"] == "tool_call"), None)
    check("the planned call is still reported", call_ev is not None)
    check("…flagged as test mode", bool(call_ev and call_ev.get("test_mode")))


def test_unknown_tool_does_not_kill_the_turn() -> None:
    print("\na direct call naming a tool that isn't registered is skipped, not fatal")
    ran.clear()
    out, client, _, _ = run([("no_such_tool", {})], tools=["fake_device"])
    check("the turn still produced an answer", out == "NARRATED", f"-> {out!r}")
    check("the model was still called", len(client.calls) == 1)


if __name__ == "__main__":
    _register_fakes()
    test_call_runs_before_the_first_model_call()
    test_transcript_stays_well_formed()
    test_summary_short_circuits_with_zero_model_calls()
    test_confirm_tier_still_raises_the_card()
    test_direct_bulk_reminder_card_has_exact_preview()
    test_denied_confirm_does_not_run_and_keeps_thinking_on()
    test_test_mode_executes_nothing()
    test_unknown_tool_does_not_kill_the_turn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
