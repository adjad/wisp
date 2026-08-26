"""`short_circuit_tools` must only fire when the tool result IS the whole answer.

The short-circuit ends a turn early and returns a tool's own output verbatim,
skipping the model's narration pass. That is correct for summarize_emails /
summarize_messages, which already synthesize real prose internally — the
narration would just restate them.

Its premise is "this tool's result is the complete answer". VERIFIED BROKEN
2026-08-09: the condition checked only `len(tool_calls) == 1`, with no awareness
of what else had already run this turn. Two ways that goes wrong, both reachable:

  * the aggregate to-do route (`multi_round=True`, offering all four sources) —
    if the model emits summarize_emails ALONE in a step, the turn ends with the
    mail digest as the entire answer to "what do I need to do", and the calendar,
    notes and messages are never consulted. SYSTEM's own words: "an answer from
    one of them is not a partial answer, it is a WRONG one." SYSTEM also warns
    the model against calling these "one at a time over several turns" — i.e.
    the triggering behaviour is known to happen.
  * ANY route where an earlier step already ran a different tool (get_upcoming
    in step 0, summarize_messages in step 1) — the calendar result is silently
    discarded.

    .venv/bin/python tests/test_short_circuit.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402
from service.tools.registry import REGISTRY, Tool  # noqa: E402

PASS, FAIL = 0, 0
SUMMARY_TEXT = "Here is a warm summary of your mail, already written as prose."
OTHER_TEXT = "calendar: Dentist at 3pm"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def _register_fakes() -> None:
    """Two read-only fakes: one 'pre-synthesized' summary tool, one ordinary
    read. fs_read is an ALLOW-tier category, so neither hits a confirm gate."""
    for name, out in (("fake_summary", SUMMARY_TEXT), ("fake_other", OTHER_TEXT)):
        REGISTRY[name] = Tool(
            name=name, description=f"fake {name}",
            parameters={"type": "object", "properties": {}},
            category="fs_read", func=(lambda _o=out: _o))


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
            yield {"kind": "content", "text": "NARRATED ANSWER"}
            yield {"kind": "final", "message": {"role": "assistant",
                                               "content": "NARRATED ANSWER",
                                               "tool_calls": None}}


class Approver:
    async def confirm(self, action):
        return True


async def emit(ev):
    return None


def run(script, **kw) -> str:
    c = ScriptedClient(script)
    return asyncio.run(loop.run_agent(
        c, "Agents-A1-4B-oQe6", [{"role": "user", "content": "what do I need to do?"}],
        emit, Approver(), tools=["fake_summary", "fake_other"], max_steps=4,
        short_circuit_tools={"fake_summary"}, **kw))


# --------------------------------------------------------------------------

def test_short_circuit_still_works_for_the_case_it_exists_for() -> None:
    print("\nthe intended case: one summary tool, nothing else -> return it verbatim")
    out = run([["fake_summary"]])
    check("returns the tool's own prose, not a narration",
          out == SUMMARY_TEXT, f"-> {out!r}")


def test_multi_round_route_never_short_circuits() -> None:
    print("\na multi_round route must NOT end on one of its several sources")
    out = run([["fake_summary"], None], multi_round=True)
    check("does not return the lone mail digest as the whole answer",
          out != SUMMARY_TEXT, f"-> {out!r}")
    check("goes on to a narration step instead",
          out == "NARRATED ANSWER", f"-> {out!r}")


def test_earlier_tool_in_a_previous_step_blocks_it() -> None:
    print("\nan earlier step's result must not be silently discarded")
    # get_upcoming-shaped read in step 0, summary tool alone in step 1. Not
    # multi_round, so `tools_answered <= {name}` is what has to catch this.
    out = run([["fake_other"], ["fake_summary"], None])
    check("does not short-circuit away the earlier result",
          out != SUMMARY_TEXT, f"-> {out!r}")
    check("narrates both instead", out == "NARRATED ANSWER", f"-> {out!r}")


def test_compound_step_still_blocked() -> None:
    print("\ntwo tools in the SAME step still need merging (pre-existing rule)")
    out = run([["fake_summary", "fake_other"], None])
    check("does not short-circuit a compound read",
          out == "NARRATED ANSWER", f"-> {out!r}")


if __name__ == "__main__":
    _register_fakes()
    test_short_circuit_still_works_for_the_case_it_exists_for()
    test_multi_round_route_never_short_circuits()
    test_earlier_tool_in_a_previous_step_blocks_it()
    test_compound_step_still_blocked()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
