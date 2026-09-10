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
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402
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

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def ensure_only(self, model, *, exclusive=False, emit=None, **kw):
        return None

    async def stream_events(self, model, messages, *, tools=None, tool_choice=None,
                            temperature=None, max_tokens=2048, **extra):
        self.calls.append(list(messages))
        yield {"kind": "content", "text": "NARRATED"}
        yield {"kind": "final", "message": {"role": "assistant", "content": "NARRATED",
                                            "tool_calls": None}}


class Approver:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.seen: list[dict] = []

    async def confirm(self, action):
        self.seen.append(action)
        return self.answer


def run(direct, *, approver=None, short_circuit=None, test_mode=False, tools=None):
    client = ScriptedClient()
    approver = approver or Approver()
    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    out = asyncio.run(loop.run_agent(
        client, "Agents-A1-4B-oQe6", [{"role": "user", "content": "go"}],
        emit, approver,
        tools=tools if tools is not None else [n for n, _ in direct],
        max_steps=3, direct_calls=direct,
        short_circuit_tools=short_circuit, test_mode=test_mode))
    return out, client, approver, events


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
