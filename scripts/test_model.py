#!/usr/bin/env python3
"""Try any installed model against Wisp's real pipeline, on the fly.

Runs the ACTUAL router + agent loop + tool registry — not a mock — so a pass
here means the model genuinely drives Wisp, and a failure points at the stage
that broke.

    python scripts/test_model.py the summarizer model
    python scripts/test_model.py --all                 # every installed chat model
    python scripts/test_model.py <model> --write       # include mutating tasks
    python scripts/test_model.py <model> --only calendar,notes
    python scripts/test_model.py <model> --promote     # make it the agent model

Read-only by default: every task that would change something is skipped unless
--write is passed, and anything not on the task's own expected-tool list is
denied at the confirmation gate even then.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A full run is minutes long and is usually watched through a redirect or a
# pipe, where Python's default block buffering holds every line until exit —
# which looks exactly like a hung run. Line buffering keeps it a live progress
# report without callers needing to remember `python -u`.
sys.stdout.reconfigure(line_buffering=True)

from service.agent.loop import run_agent          # noqa: E402
from service.config import (  # noqa: E402
    models_config, role_to_model, set_role)
from service.main import ROLE_SYSTEM                # noqa: E402
from service.inference.omlx_client import OMLXClient  # noqa: E402
from service.router.router import route           # noqa: E402


# A tool reporting its own failure. Anchored to the leading "(" these tools use
# for error returns, so an ordinary answer that happens to say "error" (e.g. a
# log file being read back) isn't misread as a broken tool.
_TOOL_ERROR_RE = re.compile(
    r"\((?:could not|couldn't|unknown tool|unable to|failed|.{0,24}\berror\b)", re.I)


@dataclass
class Task:
    name: str
    group: str
    prompt: str
    # tools that count as a correct call (first match wins)
    expect: tuple[str, ...]
    writes: bool = False
    # substrings that must appear in the final answer (case-insensitive)
    must_contain: tuple[str, ...] = field(default_factory=tuple)
    # When set, bypass the router and issue a direct chat completion under this
    # role's ROLE_SYSTEM prompt — the shape main.py uses for coding/reasoning.
    # Keyed off the task rather than the router decision on purpose: the router
    # sends several of these prompts to role=agent/needs_tools=True, but the
    # question being asked here is "can this model SERVE the coding role", which
    # is a property of the role assignment, not of one prompt's classification.
    direct_role: str | None = None


TASKS: list[Task] = [
    # ---- read-only ----------------------------------------------------
    Task("volume",        "system",   "What's the current volume?", ("get_volume",)),
    Task("battery",       "system",   "How's my battery health?", ("get_battery_status",)),
    Task("calendar_read", "calendar", "What's on my calendar this week?", ("get_upcoming",)),
    Task("calendar_past", "calendar", "What did I have on my calendar last week?",
         ("get_past_events", "get_upcoming")),
    Task("reminders",     "reminders", "What are my reminders?", ("get_upcoming",)),
    Task("notes_broad",   "notes",    "What's in my notes?", ("search_notes",)),
    Task("notes_query",   "notes",    "Search my notes for latte", ("search_notes",)),
    Task("contact_one",   "contacts", "What's Mom's phone number?",
         ("lookup_contact", "list_contacts")),
    Task("contact_list",  "contacts", "How many contacts do I have?",
         ("list_contacts", "lookup_contact")),
    Task("files_list",    "files",    "List the files in my Downloads folder.", ("list_dir",)),
    Task("files_read",    "files",    "Read the first few lines of ~/.zshrc",
         ("read_file", "run_shell")),
    Task("email_read",    "email",    "Summarize my recent emails.",
         ("summarize_emails", "view_emails")),
    Task("messages_read", "messages", "What are my recent messages about?",
         ("summarize_messages", "view_messages")),
    # Regression: vague self-referential phrasing with no obvious tool match
    # used to fall through to the ambiguous full-toolset default route, where
    # a small model (verified on LFM2.5-2.6B) borrowed OTHER tools' argument
    # names for a memory-lookup tool (e.g. search_notes'/recall's
    # `query`/`count`) and misnamed summarize_emails' `day` as `days` — every
    # call failed, and burning the whole step budget on it meant the raw
    # TypeError-derived string became the "final answer". See SELF_QUERY_RE in
    # service/router/router.py.
    Task("self_vague", "memory", "What do you know about my finances?",
         ("recall",)),
    Task("memory_recall", "memory", "What do you remember about me?", ("recall",)),
    Task("shell_calc",    "compute",  "Use the shell to compute the SHA-256 of the text 'wisp'.",
         ("run_shell",)),
    Task("web",           "web",      "What's the current price of Bitcoin?", ("web_fetch",)),
    Task("no_tool",       "chitchat", "Say hello in exactly three words.", ()),

    # ---- non-agentic roles ----------------------------------------------
    # coding/reasoning do NOT run through the agent loop in production (see
    # main.py's `elif decision.role == "reasoning"` branch and the streaming
    # general/coding branch below it) — they're direct chat completions with
    # ROLE_SYSTEM prompts. run_task dispatches to the same shape, so promoting
    # a model into those roles is tested the way it will actually be used.
    Task("code_fn",       "coding",
         "Write a Python function `add_one(nums)` that returns a new list with "
         "each integer incremented by 1. Output only the code.",
         (), must_contain=("def add_one", "return"), direct_role="coding"),
    Task("code_fenced",   "coding",
         "Write a Python function that reverses a string.",
         (), must_contain=("def", "```"), direct_role="coding"),
    # Verifiable answers — a confident wrong number is the failure mode that
    # matters for this role, so both have exactly one checkable result.
    Task("reason_trap",   "reasoning",
         "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than "
         "the ball. How much does the ball cost?",
         (), must_contain=("0.05",), direct_role="reasoning"),
    Task("reason_steps",  "reasoning",
         "I have 3 boxes. The first holds 12 apples, the second holds twice the "
         "first, and the third holds 7 fewer than the second. How many apples "
         "in total? Give the number.",
         (), must_contain=("53",), direct_role="reasoning"),

    # ---- mutating (only with --write) -----------------------------------
    Task("calendar_add",  "calendar",
         "Add a calendar event called 'WISPTEST delete me' tomorrow at 3pm.",
         ("add_calendar_event",), writes=True),
    Task("reminder_add",  "reminders",
         "Set a reminder to 'WISPTEST delete me' tomorrow at 4pm.",
         ("add_reminder",), writes=True),
    Task("calendar_cancel", "calendar",
         "Cancel the event called 'WISPTEST delete me'.",
         ("cancel_event",), writes=True),
    Task("open_app",      "apps",     "Open the Calculator app.", ("open_app",), writes=True),
    Task("quit_app",      "apps",     "Quit the Calculator app.", ("quit_app",), writes=True),
]


class Approver:
    """Approves only the tools this task expected; denies anything else.

    A model that goes off-script (deleting a file when asked to open an app)
    gets denied rather than executed, and the denial is recorded as a failure.
    """

    def __init__(self, allowed: tuple[str, ...]) -> None:
        self.allowed = set(allowed)
        self.denied: list[str] = []
        self.approved: list[str] = []

    async def confirm(self, action: dict) -> bool:
        tool = action.get("tool", "")
        if tool in self.allowed:
            self.approved.append(tool)
            return True
        self.denied.append(tool)
        return False


@dataclass
class Result:
    task: Task
    ok: bool
    detail: str
    seconds: float
    calls: list[str]
    routed_model: str
    final: str
    # True when the router never offered the tool the task needed — a Wisp
    # routing fault that every model fails identically, not a model weakness.
    route_fault: bool = False


async def preflight(client: OMLXClient, model: str) -> str | None:
    """Return an error string if `model` can't actually be used.

    Catches the silent failure mode: oMLX accepts a load for an unknown model
    id, ensure_only polls until it times out, and returns normally with nothing
    resident — every later call then runs against a model that isn't there.
    """
    try:
        installed = await client.models()
    except Exception as e:  # noqa: BLE001
        return f"oMLX unreachable: {type(e).__name__}: {e}"
    if model not in installed:
        near = [m for m in installed if model.lower()[:12] in m.lower()]
        hint = f"  close matches: {', '.join(near)}" if near else ""
        return f"not installed in oMLX.{hint}"
    try:
        await asyncio.wait_for(client.ensure_only(model, exclusive=True), timeout=180)
    except asyncio.TimeoutError:
        return "load timed out after 180s"
    except Exception as e:  # noqa: BLE001
        return f"load failed: {type(e).__name__}: {e}"
    if model not in await client.loaded_models():
        return ("load reported success but the model is NOT resident "
                "(oMLX accepted the request and silently dropped it)")
    return None


async def run_task(client: OMLXClient, model: str, task: Task,
                   timeout: float) -> Result:
    decision = await route(task.prompt)
    events: list[dict] = []

    async def emit(ev: dict) -> None:
        events.append(ev)

    approver = Approver(task.expect)
    t0 = time.time()

    # Non-agentic roles (coding / reasoning / plain general chat) never touch
    # the agent loop in production — main.py answers them with a direct chat
    # completion under a ROLE_SYSTEM prompt. Mirror that here so a model
    # promoted into those roles is exercised the way it will really be used;
    # running them through run_agent instead would test a path that never runs.
    if task.direct_role:
        role = task.direct_role
        try:
            await client.ensure_only(model, exclusive=True)
            sysp = ROLE_SYSTEM.get(role, ROLE_SYSTEM["general"])
            resp = await asyncio.wait_for(
                client.chat(model,
                            [{"role": "system", "content": sysp},
                             {"role": "user", "content": task.prompt}],
                            max_tokens=4000,
                            temperature=0.3 if role == "reasoning" else 0.0),
                timeout=timeout)
        except asyncio.TimeoutError:
            return Result(task, False, f"timed out after {timeout:.0f}s",
                          time.time() - t0, [], decision.model, "")
        except Exception as e:  # noqa: BLE001
            return Result(task, False, f"{type(e).__name__}: {str(e)[:160]}",
                          time.time() - t0, [], decision.model, "")
        dt = time.time() - t0
        msg = resp["choices"][0]["message"]
        final = (msg.get("content") or "").strip()
        # Some models keep the whole answer in reasoning_content and leave
        # content empty — main.py has the same fallback.
        if not final:
            final = (msg.get("reasoning_content") or "").strip()
        if not final:
            return Result(task, False, "empty answer", dt, [], decision.model, "")
        missing = [s for s in task.must_contain if s.lower() not in final.lower()]
        if missing:
            return Result(task, False, f"answer missing {missing}", dt, [],
                          decision.model, final)
        return Result(task, True, f"direct chat ({role})", dt, [],
                      decision.model, final)

    try:
        await client.ensure_only(model, exclusive=not decision.tool_subset)
        final = await asyncio.wait_for(
            run_agent(client, model, [{"role": "user", "content": task.prompt}],
                      emit, approver,
                      tools=decision.tool_subset,
                      force_first_tool=decision.force_first_tool,
                      expect_tool_first=decision.expect_tool_first,
                      max_steps=6, max_tokens=4000,
                      temperature=0.6 if decision.tool_subset else 0.0,
                      multi_round=decision.multi_round,
                      narration_after=decision.narration_after),
            timeout=timeout)
    except asyncio.TimeoutError:
        return Result(task, False, f"timed out after {timeout:.0f}s", time.time() - t0,
                      [], decision.model, "")
    except Exception as e:  # noqa: BLE001
        return Result(task, False, f"{type(e).__name__}: {str(e)[:160]}",
                      time.time() - t0, [], decision.model, "")

    dt = time.time() - t0
    calls = [e["name"] for e in events if e.get("type") == "tool_call"]
    final = (final or "").strip()

    # A tool the router never offered this turn — the loop rejects these, and a
    # model that keeps reaching for them isn't usable on the narrow routes.
    unoffered = sum(1 for e in events if e.get("type") == "tool_result"
                    and "is not available for this request" in str(e.get("result", "")))

    # Distinguish "this model is bad" from "the router never gave it the tool".
    # A light-read route restricts the loop to decision.tool_subset, so a model
    # that fails a task whose expected tool was never offered is not the thing
    # at fault — and reading that as a model failure is exactly how a routing
    # bug gets misdiagnosed as "the new model doesn't work".
    offered = set(decision.tool_subset) if decision.tool_subset else None
    unofferable = offered is not None and not (offered & set(task.expect)) and task.expect

    if task.expect:
        if unofferable:
            return Result(task, False,
                          f"ROUTE: router offered {sorted(offered)}, "
                          f"never {task.expect[0]}",
                          dt, calls, decision.model, final, route_fault=True)
        if not calls:
            return Result(task, False, f"called no tool (wanted {task.expect[0]})",
                          dt, calls, decision.model, final)
        if not set(calls) & set(task.expect):
            return Result(task, False,
                          f"called {calls} instead of {task.expect[0]}",
                          dt, calls, decision.model, final)
    elif calls:
        return Result(task, False, f"called {calls} when no tool was needed",
                      dt, calls, decision.model, final)

    if approver.denied:
        return Result(task, False, f"tried unapproved {approver.denied}", dt, calls,
                      decision.model, final)

    # The model calling the right tool is only half the test — the tool has to
    # have WORKED. Tools in this codebase report failure as a parenthesised
    # string ("(could not quit 'Calculator': ...)") and otherwise return plain
    # prose, so a leading "(" plus a failure word is a reliable signal without
    # tripping on ordinary answers that merely contain the word "error".
    # Without this the suite scored a PASS on quit_app while it was failing for
    # every app name (see service/tools/apps.py's _as_str) — the model did
    # everything right and the tool underneath was broken.
    broken = [str(e.get("result", "")) for e in events
              if e.get("type") == "tool_result"
              and _TOOL_ERROR_RE.match(str(e.get("result", "")).strip())]
    if broken:
        return Result(task, False, f"tool returned an error: {broken[0][:110]}",
                      dt, calls, decision.model, final)
    if not final:
        return Result(task, False, "empty final answer", dt, calls, decision.model, final)
    missing = [s for s in task.must_contain if s.lower() not in final.lower()]
    if missing:
        return Result(task, False, f"answer missing {missing}", dt, calls,
                      decision.model, final)
    extra = f" (+{unoffered} unoffered)" if unoffered else ""
    return Result(task, True, f"{calls or 'direct answer'}{extra}", dt, calls,
                  decision.model, final)


async def test_model(client: OMLXClient, model: str, tasks: list[Task],
                     timeout: float, verbose: bool) -> list[Result]:
    print(f"\n\033[1m{'=' * 78}\n{model}\n{'=' * 78}\033[0m")
    err = await preflight(client, model)
    if err:
        print(f"  \033[31mPREFLIGHT FAILED\033[0m — {err}")
        return [Result(t, False, "preflight failed", 0.0, [], "", "") for t in tasks]

    results: list[Result] = []
    for task in tasks:
        r = await run_task(client, model, task, timeout)
        results.append(r)
        mark = ("\033[32m PASS\033[0m" if r.ok
                else ("\033[35mROUTE\033[0m" if r.route_fault else "\033[31m FAIL\033[0m"))
        print(f" {mark} {task.group:9} {task.name:15} [{r.seconds:5.1f}s] {r.detail}")
        if verbose or not r.ok:
            snippet = r.final.replace("\n", " ")[:150]
            if snippet:
                print(f"          -> {snippet!r}")
    passed = sum(1 for r in results if r.ok)
    routef = sum(1 for r in results if r.route_fault)
    total = len(results)
    slow = sum(r.seconds for r in results) / max(total, 1)
    scoreable = total - routef
    colour = ("\033[32m" if passed == scoreable
              else ("\033[33m" if passed >= scoreable * 0.7 else "\033[31m"))
    extra = f" · {routef} router fault(s) excluded" if routef else ""
    print(f"  {colour}{passed}/{scoreable} passed\033[0m · avg {slow:.1f}s/task{extra}")
    return results


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="*", help="model ids to test")
    ap.add_argument("--all", action="store_true", help="test every installed chat model")
    ap.add_argument("--write", action="store_true",
                    help="include tasks that create/cancel events and open apps")
    ap.add_argument("--only", default="", help="comma-separated groups, e.g. notes,calendar")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-task timeout (s)")
    ap.add_argument("--verbose", action="store_true", help="show answers for passing tasks too")
    ap.add_argument("--promote", action="store_true",
                    help="on a clean run, point the general+agent roles at the model")
    args = ap.parse_args()

    client = OMLXClient()
    models = list(args.models)
    if args.all:
        skip = ("Embedding", "ocr", "VL")   # not chat models
        models = [m for m in await client.models() if not any(s in m for s in skip)]
    if not models:
        ap.error("give at least one model id, or --all")

    tasks = [t for t in TASKS if args.write or not t.writes]
    if args.only:
        groups = {g.strip() for g in args.only.split(",")}
        tasks = [t for t in tasks if t.group in groups]
    if not tasks:
        ap.error("no tasks matched --only")

    print(f"agent role currently: \033[1m{role_to_model('agent')}\033[0m")
    print(f"{len(tasks)} tasks × {len(models)} model(s)"
          f"{' · WRITE ENABLED' if args.write else ' · read-only'}")

    summary: dict[str, tuple[int, int]] = {}
    for model in models:
        results = await test_model(client, model, tasks, args.timeout, args.verbose)
        summary[model] = (sum(1 for r in results if r.ok),
                          len(results) - sum(1 for r in results if r.route_fault))

    if len(models) > 1:
        print(f"\n\033[1m{'=' * 78}\nSUMMARY\n{'=' * 78}\033[0m")
        for model, (p, t) in sorted(summary.items(), key=lambda kv: -kv[1][0]):
            bar = "█" * round(20 * p / max(t, 1))
            print(f"  {p:2}/{t:<2} {bar:<20} {model}")

    if args.promote:
        model = models[0]
        p, t = summary[model]
        if p != t:
            print(f"\n\033[33mnot promoted\033[0m — {model} failed {t - p} task(s)")
        else:
            set_role("general", model)   # also repoints `agent`
            print(f"\n\033[32mpromoted\033[0m {model} → general + agent roles")
            # The `coding` role is NOT repointed automatically — a dedicated
            # coding specialist is a legitimate setup. But leaving it on the old
            # model has a non-obvious cost worth naming: a plain, non-agentic
            # code question (rule_route's CODE_RE match, no tools needed) routes
            # straight to role_to_model("coding") — so a split that was dormant
            # while both roles pointed at the agent model REACTIVATES here, and
            # every such request now swaps the old heavy model in and back out.
            coding = role_to_model("coding")
            if coding != model:
                print(f"  \033[33mnote\033[0m `coding` is still {coding} — code requests "
                      f"will swap it in mid-turn.")
                print(f"       match it with:  python scripts/test_model.py {model} "
                      f"--only compute,files\n"
                      f"       then set it in Settings, or POST /config "
                      f'{{"role":"coding","model":"{model}"}}')

    await client.aclose()
    return 0 if all(p == t for p, t in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
