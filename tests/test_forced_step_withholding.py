"""A tool withheld by a FORCED step must not read as an unavailable capability.

Reported live 2026-08-16 ("organize all of the wisp debug logs into one folder
and sorted by date"). Three separate faults stacked into one wrong answer:

  1. The single word `debug` matched CODE_AUTHOR_RE *and* CODE_RE — it is in
     both lists — so it misread as a code-authoring request off one noun
     phrase.
  2. That forced a single narrow tool onto step 0 of a route whose real subset
     is [read_file, list_dir, write_file, run_shell]. Step 0 therefore
     advertised exactly one tool, and it was not one the route had granted.
  3. The model called run_shell anyway — correctly, it is in the route's own
     subset — and the loop rejected it with "run_shell is not available for
     this request". The model believed it, hard_failed latched, and it told the
     user "I don't have the ability to search your file system", while the same
     work split into single steps succeeded.

The code-delegation machinery (needs_code_delegation / write_code /
force_save_next) that caused #2 was removed entirely once Coder-14B was
retired and `coding == agent` made it a same-model round-trip for nothing —
see codegen.py's removal. What's still tested here: the CODE_AUTHOR_RE/CODE_RE
disambiguation from #1 (still live in _domain_subset's bailout), and the
general forced-step withholding mechanism from #3 (still live for any
force_first_tool, exercised below via list_dir rather than write_code).

    .venv/bin/python tests/test_forced_step_withholding.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402
from service.router.router import _finalize, rule_route  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def route(text: str):
    d = rule_route(text)
    return None if d is None else _finalize(d, text)


class ScriptedClient:
    """Answers with a queue of canned messages, recording what was offered."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.offered: list[list[str]] = []

    async def ensure_only(self, model, *, exclusive=False, emit=None, **kw):
        return None

    async def stream_events(self, model, messages, *, tools=None, tool_choice=None,
                            temperature=None, max_tokens=2048, **extra):
        self.offered.append([t["function"]["name"] for t in (tools or [])])
        msg = self.replies.pop(0) if self.replies else {
            "role": "assistant", "content": "done", "tool_calls": None}
        yield {"kind": "final", "message": msg}


class Approver:
    async def confirm(self, action):
        return True


def call(name, args="{}", cid="c1"):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name, "arguments": args}}]}


def run_loop(replies, **kw):
    results: list[str] = []

    async def emit(ev):
        if ev.get("type") == "tool_result":
            results.append(ev["result"])

    c = ScriptedClient(replies)
    text = asyncio.run(loop.run_agent(
        c, "Agents-A1-4B-oQe6",
        [{"role": "user", "content": "organize my wisp debug logs by date"}],
        emit, Approver(), max_steps=3, **kw))
    return c, results, text


print(__doc__.split("\n")[0])

print("\nthe router no longer reads 'debug logs' as a request to author code")
_prompt = "i need you to organize all of the wisp debug logs into one folder and sorted by date"
d = route(_prompt)
# _DOCUMENT_TOOLS is gone (2026-08-21) — this route now fills its tools by
# semantic retrieval (see router.py's _mk_scoped `subset=None` doc), so the
# sync rule_route() plus _finalize() now also pins the non-destructive move
# alternatives as an execution-contract obligation.  Semantic retrieval still
# fills the rest of the menu in the real async route below.
check("takes the reorganize-files route",
      d is not None and d.reason == "reorganize files"
      and {"move_path", "organize_files"}.issubset(set(d.tool_subset or [])),
      str(d and (d.tool_subset, d.reason)))
check("…and that route is retrieval-scoped, not the ambiguous default",
      d is not None and d.multi_round_on_retrieval is True,
      str(d and d.multi_round_on_retrieval))

from service.router.router import route as _async_route  # noqa: E402
d_retrieved = asyncio.run(_async_route(_prompt))
# The safety property is "a REAL MOVE tool is offered" (shutil.move, never rm),
# not specifically move_path — organize_files is the same category (fs_write,
# confirm-gated) and shares the invariant (verified live, not assumed: on this
# exact "wisp debug logs" prompt retrieval surfaces organize_files, not
# move_path, and it's a BETTER fit here — its own aliases literally include
# "move all the wisp logs into a logs folder"; move_path's own description
# says "for moving ONE specific file", which this bulk-pattern request isn't).
_move_tools = {"move_path", "organize_files"}
check("…and retrieval surfaces a real move tool, so it never needs rm to reorganize",
      bool(_move_tools & set(d_retrieved.tool_subset or [])), str(d_retrieved.tool_subset))
check("run_shell is on the table (pinned escape hatch)",
      "run_shell" in (d_retrieved.tool_subset or []), str(d_retrieved.tool_subset))

print("\nthe verb sense of 'debug' is untouched")
for t in ["debug my python script", "debug the login function"]:
    from service.router.router import CODE_RE
    check(f"still reads as code: {t}", bool(CODE_RE.search(t)))
check("'debug logs' does not", not CODE_RE.search("move my debug logs"))
check("'debug mode' does not", not CODE_RE.search("turn on debug mode"))

print("\na plural-noun filesystem chore reaches the tool routes at all")
d = route("move my debug logs into folders by date")
check("gets tools", d is not None and d.needs_tools, str(d and d.reason))

print("\na tool withheld by a forced step is rejected as PREMATURE, not missing")
c, results, text = run_loop(
    [call("run_shell", '{"command": "ls ~/Downloads"}'),
     {"role": "assistant", "content": "ok", "tool_calls": None}],
    tools=["read_file", "list_dir", "run_shell"], force_first_tool="list_dir")
check("step 0 offered only the forced tool", c.offered[0] == ["list_dir"], str(c.offered[:1]))
check("the rejection says it IS available",
      bool(results) and "IS available" in results[0], str(results[:1]))
check("it does NOT claim the action is unavailable",
      bool(results) and "is not available for this request" not in results[0], str(results[:1]))
check("it names the next step as the place to call it",
      bool(results) and "next step" in results[0], str(results[:1]))
check("the tool is offered again on the next step",
      len(c.offered) > 1 and "run_shell" in c.offered[1], str(c.offered[1:2]))

print("\na tool the turn never granted is still a hard no")
c, results, text = run_loop(
    [call("delete_path", '{"path": "~/Downloads"}'),
     {"role": "assistant", "content": "ok", "tool_calls": None}],
    tools=["read_file", "list_dir", "run_shell"], force_first_tool="list_dir")
check("rejected as unavailable",
      bool(results) and "is not available for this request" in results[0], str(results[:1]))
check("not softened into 'call it next step'",
      bool(results) and "next step" not in results[0], str(results[:1]))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
