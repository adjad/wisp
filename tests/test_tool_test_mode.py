"""run_agent(test_mode=True) must plan tool use WITHOUT ever running a tool.

The whole point of tool test mode is "tell me what you'd do, don't do it" —
so this asserts the two things that actually matter:

  * no tool's real function ever executes, regardless of its safety tier
    (fs_read/ALLOW and email_send/CONFIRM are both covered — CONFIRM matters
    more, since a bug there would mean a dry run could still ask the user to
    approve a real send)
  * approver.confirm() is never reached — test mode intercepts BEFORE the
    tier decision would normally route to it, so a confirm-tier tool must
    never even raise the card
  * the model can still chain multiple tool "calls" across steps using the
    stubbed-back placeholder result, and its final answer is a genuine plan,
    not a real one — and short_circuit_tools (which would otherwise return a
    single tool's raw result verbatim) must not fire on a stub

    .venv/bin/python tests/test_tool_test_mode.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402
from service.tools.registry import REGISTRY, Tool  # noqa: E402

PASS, FAIL = 0, 0
PLAN_TEXT = "PLAN: I would read the file, then send the email."

STATE = {"read_called": False, "send_called": False}


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def _register_fakes() -> None:
    def _read():
        STATE["read_called"] = True
        return "the file's real contents"

    def _send():
        STATE["send_called"] = True
        return "sent!"

    REGISTRY["fake_read"] = Tool(
        name="fake_read", description="fake read-only tool",
        parameters={"type": "object", "properties": {}},
        category="fs_read", func=_read)
    REGISTRY["fake_send"] = Tool(
        name="fake_send", description="fake outbound-send tool",
        parameters={"type": "object", "properties": {}},
        category="email_send", func=_send)


class ScriptedClient:
    """Emits a scripted sequence of tool calls, then a plain text answer."""

    def __init__(self, script: list[list[str] | None]) -> None:
        self.script = script
        self.i = 0

    async def ensure_only(self, model, *, exclusive=False, emit=None, **kw):
        return None

    async def stream_events(self, model, messages, *, tools=None, tool_choice=None,
                            temperature=None, max_tokens=2048, **extra):
        names = self.script[self.i] if self.i < len(self.script) else None
        self.i += 1
        if names:
            calls = [{"id": f"c{j}", "function": {"name": n, "arguments": "{}"}}
                     for j, n in enumerate(names)]
            yield {"kind": "final", "message": {"role": "assistant", "content": "",
                                               "tool_calls": calls}}
        else:
            yield {"kind": "content", "text": PLAN_TEXT}
            yield {"kind": "final", "message": {"role": "assistant",
                                               "content": PLAN_TEXT,
                                               "tool_calls": None}}


class NeverConfirmApprover:
    """Fails loudly if test mode ever reaches a real confirmation gate."""

    async def confirm(self, action):
        raise AssertionError(
            f"approver.confirm() was called in test mode for {action!r} — "
            "a dry run must never reach the real confirm gate")


EVENTS: list[dict] = []


async def emit(ev):
    EVENTS.append(ev)


def run(script, **kw) -> str:
    EVENTS.clear()
    STATE["read_called"] = False
    STATE["send_called"] = False
    c = ScriptedClient(script)
    tool_names = kw.pop("tools", ["fake_read", "fake_send"])
    return asyncio.run(loop.run_agent(
        c, "Agents-A1-4B-oQe6", [{"role": "user", "content": "read the file and email it to mom"}],
        emit, NeverConfirmApprover(), tools=tool_names, max_steps=4,
        test_mode=True, **kw))


# --------------------------------------------------------------------------

def test_nothing_actually_runs() -> None:
    print("\nboth a read (ALLOW) and a send (CONFIRM) tool are 'called' in one step")
    out = run([["fake_read", "fake_send"], None])
    check("fake_read's real function never executed", STATE["read_called"] is False)
    check("fake_send's real function never executed", STATE["send_called"] is False)
    check("final answer is the model's plan, not a real result",
          out == PLAN_TEXT, f"-> {out!r}")


def test_reports_correct_safety_tiers() -> None:
    print("\nthe plan reports what WOULD have happened per tool")
    run([["fake_read", "fake_send"], None])
    calls = {e["name"]: e for e in EVENTS if e.get("type") == "tool_call"}
    check("fake_read planned as allow-tier", calls["fake_read"]["decision"] == "allow",
          f"-> {calls.get('fake_read')}")
    check("fake_send planned as confirm-tier", calls["fake_send"]["decision"] == "confirm",
          f"-> {calls.get('fake_send')}")
    check("both tool_call events are flagged test_mode",
          calls["fake_read"].get("test_mode") is True
          and calls["fake_send"].get("test_mode") is True)
    check("no real confirm card was ever raised",
          not any(e.get("type") == "confirm" for e in EVENTS))


def test_multi_step_chaining_via_stub() -> None:
    print("\nthe model can still chain a second step off the stubbed-back result")
    out = run([["fake_read"], ["fake_send"], None])
    check("neither tool executed across two steps",
          STATE["read_called"] is False and STATE["send_called"] is False)
    check("still reaches a final plan", out == PLAN_TEXT, f"-> {out!r}")


def test_short_circuit_does_not_fire_on_a_stub() -> None:
    print("\nshort_circuit_tools must not return the stub as if it were real content")
    out = run([["fake_read"], None], short_circuit_tools={"fake_read"})
    check("does not return the placeholder stub verbatim",
          out != loop._TEST_MODE_STUB, f"-> {out!r}")
    check("goes on to the model's real plan narration",
          out == PLAN_TEXT, f"-> {out!r}")


def test_execution_contract_forces_the_missing_step() -> None:
    print("\nan execution contract rejects premature final prose and forces the missing step")
    out = run([["fake_read"], None, ["send_message"], None],
              tools=["fake_read", "send_message"],
              required_tool_groups=(frozenset({"fake_read"}),
                                    frozenset({"send_message"})))
    names = [e.get("name") for e in EVENTS if e.get("type") == "tool_call"]
    check("the missing send step is planned", names == ["fake_read", "send_message"], str(names))
    check("dry-run action cannot be narrated as success", out.startswith("Dry run only"), out)
    check("the real send never executes", STATE["send_called"] is False)


if __name__ == "__main__":
    _register_fakes()
    test_nothing_actually_runs()
    test_reports_correct_safety_tiers()
    test_multi_step_chaining_via_stub()
    test_short_circuit_does_not_fire_on_a_stub()
    test_execution_contract_forces_the_missing_step()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
