"""When the model gives no answer, the fallback must not drop the other sources.

`run_agent` has two paths that answer with a tool's raw output because the model
produced nothing usable: the empty-final-answer path, and the step-limit exit.
Both used `last_tool_result` — a single string overwritten by every tool.

That is a wrong-answer bug on any route that reads several sources. The
aggregate to-do route calls get_upcoming AND search_notes AND summarize_emails
AND summarize_messages in ONE step; falling back to "the last one" hands the
user the messages digest as their entire to-do list, committing precisely the
error SYSTEM spends twenty lines forbidding: "an answer from one of them is not
a partial answer, it is a WRONG one — it quietly implies the other four were
empty." The step-limit exit has the same shape and is reached on exactly the
multi-hop turns where several sources have already answered.

The data was already in hand both times; it was being thrown away.

    .venv/bin/python tests/test_multi_source_fallback.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402
from service.tools.registry import REGISTRY, Tool  # noqa: E402

PASS, FAIL = 0, 0

CAL = "Dentist at 3pm on Tuesday"
NOTES = "Idea list: rebuild the deck"
MAIL = "3 unread, one from the landlord"
MSGS = "Mom asked about Sunday"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def _register() -> None:
    for name, out in (("get_upcoming", CAL), ("search_notes", NOTES),
                      ("summarize_emails", MAIL), ("summarize_messages", MSGS)):
        REGISTRY[name] = Tool(name=name, description=f"fake {name}",
                              parameters={"type": "object", "properties": {}},
                              category="fs_read", func=(lambda _o=out: _o))
    REGISTRY["broken_tool"] = Tool(
        name="broken_tool", description="fake broken",
        parameters={"type": "object", "properties": {}},
        category="fs_read", func=lambda: "(error running broken_tool: nope)")


ALL4 = ["get_upcoming", "search_notes", "summarize_emails", "summarize_messages"]


class ScriptedClient:
    """`script` entries: a list of tool names, or None for an empty answer."""

    def __init__(self, script) -> None:
        self.script, self.i = script, 0

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
            # No content, no tool calls — the "model whiffed" shape.
            yield {"kind": "final", "message": {"role": "assistant", "content": "",
                                               "tool_calls": None}}


class Approver:
    async def confirm(self, action):
        return True


async def emit(ev):
    return None


def run(script, **kw) -> str:
    return asyncio.run(loop.run_agent(
        ScriptedClient(script), "Agents-A1-4B-oQe6",
        [{"role": "user", "content": "what do I need to do?"}],
        emit, Approver(), tools=ALL4 + ["broken_tool"], **kw))


# --------------------------------------------------------------------------

def test_empty_answer_surfaces_every_source() -> None:
    print("\nempty final answer after a 4-source step -> all four, labelled")
    out = run([ALL4, None, None], max_steps=2, multi_round=True)
    for label, body in (("calendar", CAL), ("notes", NOTES),
                        ("email", MAIL), ("messages", MSGS)):
        check(f"{label} result is present", body in out, f"missing from {out!r}")
    check("each source is labelled for the user, not named by its function",
          "**From your calendar and reminders:**" in out and "get_upcoming:" not in out,
          out[:200])


def test_step_limit_surfaces_every_source() -> None:
    print("\nstep-limit exit -> all sources, not a lottery on which ran last")
    # Every step calls a tool, so the loop runs out of steps without an answer.
    out = run([["get_upcoming"], ["search_notes"]], max_steps=2)
    check("calendar kept", CAL in out, out[:160])
    check("notes kept", NOTES in out, out[:160])


def test_single_source_answer_is_unchanged() -> None:
    print("\none source -> returned verbatim, exactly as before")
    out = run([["get_upcoming"], None, None], max_steps=2)
    check("no labels added to a single result", out == CAL, f"-> {out!r}")


def test_error_results_are_never_merged_in() -> None:
    print("\nan error result is never presented as an answer")
    # is_tool_error output is written FOR THE MODEL to retry from; showing it
    # verbatim reads as Wisp reporting a traceback as its reply.
    out = run([["get_upcoming", "broken_tool"], None, None], max_steps=2)
    check("the good source is kept", CAL in out, out[:160])
    check("the error string is not shown to the user",
          "error running broken_tool" not in out, out[:200])


def test_all_errors_gives_the_stuck_message() -> None:
    print("\nnothing clean at all -> the honest 'I couldn't' message, not an error dump")
    out = run([["broken_tool"], None, None], max_steps=2)
    check("returns the stuck message", out == loop._STUCK_MESSAGE, f"-> {out!r}")


def test_repeated_tool_gets_one_block() -> None:
    print("\na tool called twice contributes ONE labelled block, not two")
    out = run([["search_notes"], ["search_notes"]], max_steps=2)
    check("only one 'From your notes' heading",
          out.count("From your notes") <= 1, f"-> {out!r}")


if __name__ == "__main__":
    _register()
    test_empty_answer_surfaces_every_source()
    test_step_limit_surfaces_every_source()
    test_single_source_answer_is_unchanged()
    test_error_results_are_never_merged_in()
    test_all_errors_gives_the_stuck_message()
    test_repeated_tool_gets_one_block()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
