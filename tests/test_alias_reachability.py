"""Systematic check for the reachability bug that keeps recurring.

Five times now (find_files, create_note, complete_reminder, find_free_time,
flag_email/forward_email), a tool shipped with correct code, a correct
description, and correct aliases — and was still unreachable for its own
stated use case, because an EXISTING REGEX RULE claimed the phrasing first and
handed back a hardcoded subset written before the new tool existed. Retrieval
never got a chance to run; the tool's aliases were irrelevant.

The flag_email case was worse than a silent miss: with no matching tool
offered, the model called a DIFFERENT tool (view_emails) to look up the
message, then told the user "I've flagged that email for follow-up" — a
fabricated success with zero tool call behind it. See
service.agent.loop.SYSTEM's "THE INVERSE IS EQUALLY SERIOUS" rule for the
defense-in-depth prompt fix; this test is the defense-in-depth CODE fix — it
catches the underlying reachability gap before it ever reaches a model.

WHAT THIS CHECKS
-----------------
For every registered tool's own aliases (the phrasings its author wrote as
"this tool is FOR these exact requests"): run it through `rule_route()` — the
pure-regex layer, no embedder, no live oMLX needed. If a rule claims the
phrasing and returns a real tool_subset, the tool itself MUST be in that
subset. A rule returning None (falls through to semantic retrieval) is fine —
that path is covered by scripts/test_retrieval.py instead.

One historical alarm alias now has an explicitly approved clarification
contract in the shared reminder workflow. It is checked against final route
state (no creation tools, obligations, or guessed arguments), not skipped or
treated as an unrestricted equivalent-tool exception.

This does NOT need the embedder or oMLX: rule_route is pure regex matching,
so this runs fully offline and fast.

    .venv/bin/python tests/test_alias_reachability.py
"""
from __future__ import annotations

import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.tools  # noqa: E402,F401  (registers every tool module)
from service.router.router import _BEHAVIOR_TOOLS_ALL, route, rule_route  # noqa: E402
from service.tools.registry import REGISTRY  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


_BEHAVIOR_PROBE = """
import asyncio, json, sys
import service.tools  # noqa: F401
from service.router.router import route
out = []
for tool, alias in json.loads(sys.argv[1]):
    d = asyncio.run(route(alias))
    offered = sorted(set(d.tool_subset or []) | {n for n, _ in d.direct_calls})
    out.append([tool, alias, d.reason, offered])
print(json.dumps(out))
"""


def _behavior_routes(pairs: list[tuple[str, str]]) -> list:
    """Behavior-learning tools exist only while behavior learning is on
    (WISP_BEHAVIOR=1 / founder demo): route() then claims their phrasings ahead
    of the regex rules, and with it off every phrase deliberately keeps its
    existing route. Tool availability is cached per process, so they are
    checked in a fresh interpreter started with the flag set."""
    import json
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "WISP_BEHAVIOR": "1"}
    proc = subprocess.run([sys.executable, "-c", _BEHAVIOR_PROBE, json.dumps(pairs)], cwd=root, env=env,
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        print(proc.stderr[-2000:])
        return []
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    checked = 0
    behavior_aliases: list[tuple[str, str]] = []
    for tool_name, tool in sorted(REGISTRY.items()):
        for alias in tool.aliases:
            if (tool_name, alias) == ("set_alarm", "set an alarm for half six tomorrow"):
                # The shared, receipt-verified reminder workflow supersedes
                # this old tool identity. This exact unsupported clock must
                # CLARIFY, not pass through an equivalent-tool allowlist.
                final = asyncio.run(route(alias))
                checked += 1
                check("legacy half-six alias remains a reminder clarification",
                      final.reminder_action == "clarify_time")
                check("unsupported clock offers only reminder inspection",
                      final.tool_subset == ["get_upcoming"])
                check("unsupported clock cannot dispatch or force creation",
                      not final.direct_calls and final.force_first_tool is None
                      and not final.expect_tool_first)
                check("unsupported clock has no creation obligations or guessed arguments",
                      not final.required_tool_groups and not final.tool_argument_bindings)
                continue
            if tool_name in _BEHAVIOR_TOOLS_ALL:
                behavior_aliases.append((tool_name, alias))
                continue
            decision = rule_route(alias)
            if decision is None:
                continue  # falls through to retrieval — covered elsewhere
            # A direct-dispatch route resolves the tool itself, not via
            # tool_subset — direct_calls is the thing to check there instead.
            offered = set(decision.tool_subset or [])
            offered |= {n for n, _ in decision.direct_calls}
            # route() fills tool_subset via semantic retrieval for any rule
            # that wants tools but named no subset of its own (see its
            # "decision.needs_tools and decision.tool_subset is None" check) —
            # rule_route() alone can't resolve these, so skip them here; they
            # are exactly what scripts/test_retrieval.py measures instead.
            if decision.needs_tools and decision.tool_subset is None and not decision.direct_calls:
                continue
            checked += 1
            check(f"{tool_name!r} reachable via its own alias {alias[:44]!r}",
                  tool_name in offered,
                  f"rule={decision.reason!r} offered={sorted(offered)}")
    routed = _behavior_routes(behavior_aliases) if behavior_aliases else []
    check("behavior-tool aliases routed with behavior learning on", len(routed) == len(behavior_aliases))
    for tool_name, alias, reason, offered in routed:
        checked += 1
        check(f"{tool_name!r} reachable via its own alias {alias[:44]!r} (behavior on)",
              tool_name in offered, f"route={reason!r} offered={offered}")
    print(f"\nchecked {checked} (tool, own-alias) pairs claimed by a regex rule")
    print(f"{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
