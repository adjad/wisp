"""Router-direct dispatch — regression tests for OPTIMIZATION_BACKLOG.md #1.

When a rule identifies not just WHICH tool but its ARGUMENTS too, the call is
already fully determined and the model's selection step (~3-3.5s of a ~5s
one-tool turn) is re-deriving a conclusion the regex already reached. The
router pre-resolves those calls into RouteDecision.direct_calls and run_agent
executes them before its first model call.

The whole risk of this mechanism is pre-resolving a call the request could have
CHANGED — a wrong pre-filled argument is worse than a slow turn, because the
model never gets a chance to notice it. So most of what follows tests the
DECLINES, not the hits.

What must keep holding:
  * a request naming exactly one zero-arg device tool dispatches it directly;
  * a request naming TWO ("battery and lock my screen") declines — step 0 is a
    thinking-off narration step once a direct call has run, so the second call
    would be silently dropped;
  * device WRITES never pre-dispatch the matching read ("turn the volume up"
    must not fire get_volume);
  * a calendar-only lookup resolves its `days` window in Python, takes the
    WIDEST window when several are named, and declines on writes, past-tense
    questions, and compound multi-domain reads;
  * a summary request dispatches only when it carries NO qualifier the
    summarizer would need an argument for;
  * a direct route never sets expect_tool_first/force_first_tool — both turn
    step 0 back into the forced selection step this exists to skip;
  * the tools OFFERED to the model are exactly the ones pre-called, so the
    narration gate's "everything called has answered" can actually be satisfied.

    .venv/bin/python tests/test_direct_dispatch.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.router import router  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def direct(prompt: str) -> list[tuple[str, dict]]:
    d = router.rule_route(prompt)
    return list(d.direct_calls) if d else []


def test_single_zero_arg_device_tool_dispatches() -> None:
    print("\none unambiguously-named zero-arg device tool -> direct call")
    for prompt, tool in [
        ("what's my battery level", "get_battery_status"),
        ("how's my battery doing", "get_battery_status"),
        ("what's my volume at", "get_volume"),
        ("what did i copy", "clipboard_read"),
        ("lock my screen", "lock_screen"),
        ("run a speed test", "run_speed_test"),
    ]:
        calls = direct(prompt)
        check(f"{prompt!r} -> {tool}",
              calls == [(tool, {})], f"got {calls}")


def test_two_named_tools_decline() -> None:
    print("\ntwo tools named -> decline (the second would be silently dropped)")
    # Step 0 becomes a thinking-off narration step once a direct call has run,
    # so pre-dispatching only the first loses the other half of the request.
    calls = direct("what's my battery and lock my screen")
    check("compound device request emits no direct call", calls == [], f"got {calls}")
    d = router.rule_route("what's my battery and lock my screen")
    # Device-control tool_subset is retrieval-filled since 2026-08-21 (see
    # router.py's _mk_scoped `subset=None` doc) — rule_route() alone (sync)
    # can only confirm the route MATCHED (tool_subset is None, reason names
    # it); whether both tools actually come back from retrieval needs the
    # real async route().
    check("…still matches the compound device-control route",
          d is not None and set(d.tool_subset or ()) == {"get_battery_status", "lock_screen"})
    d_retrieved = asyncio.run(router.route("what's my battery and lock my screen"))
    check("…and retrieval surfaces BOTH tools",
          d_retrieved.tool_subset is not None
          and "lock_screen" in d_retrieved.tool_subset
          and "get_battery_status" in d_retrieved.tool_subset,
          str(d_retrieved.tool_subset))


def test_device_writes_never_dispatch_the_read() -> None:
    print("\na device WRITE must not pre-dispatch the matching read")
    for prompt in ["turn the volume up", "set my volume to 50", "mute my mac",
                   "copy this to my clipboard", "turn up the volume please"]:
        calls = direct(prompt)
        check(f"{prompt!r} emits no direct call", calls == [], f"got {calls}")


def test_calendar_window_resolved_in_python() -> None:
    print("\ncalendar-only lookup -> get_upcoming with a Python-resolved window")
    for prompt, days in [
        ("what's on my calendar today", 1),
        ("what's on my calendar tomorrow", 2),
        ("what's on my calendar this week", 7),
        ("what's on my calendar this month", 30),
    ]:
        calls = direct(prompt)
        check(f"{prompt!r} -> get_upcoming(days={days})",
              calls == [("get_upcoming", {"days": days})], f"got {calls}")

    # Widest wins: a wider window is a superset and get_upcoming tags every row
    # relative to today, so narration can still separate them. Too NARROW would
    # silently drop events the user asked about.
    calls = direct("what's on my calendar today and tomorrow")
    check("'today and tomorrow' takes the widest window (2d)",
          calls == [("get_upcoming", {"days": 2})], f"got {calls}")

    # No window named at all -> use the broad supported horizon. A silent
    # seven-day default made Wisp claim later events were absent.
    calls = direct("what's on my calendar")
    check("no window named -> broad 60-day horizon",
          calls == [("get_upcoming", {"days": 60})], f"got {calls}")


def test_calendar_declines_writes_past_and_compound() -> None:
    print("\ncalendar direct dispatch declines the cases get_upcoming can't cover")
    # A write needs the write tools chosen by the model.
    check("'cancel my lunch' emits no direct call", direct("cancel my lunch") == [])
    check("'add a meeting tomorrow at 3' emits no direct call",
          direct("add a meeting tomorrow at 3") == [])
    # get_upcoming only ever returns FUTURE items, so a past-tense question
    # needs get_past_events too — that route is multi_round.
    past = direct("what did i have on my calendar last week")
    check("past-tense calendar question emits no direct call", past == [], f"got {past}")
    # A compound read needs the model to call the other sources as well.
    compound = direct("what's on my messages and calendar")
    check("compound multi-domain read emits no direct call", compound == [], f"got {compound}")


def test_summary_dispatches_only_when_unqualified() -> None:
    print("\nsummary dispatch requires NO qualifier the summarizer needs an argument for")
    for prompt, tool in [
        ("summarize my inbox", "summarize_emails"),
        ("summarize my email", "summarize_emails"),
        ("recap my messages", "summarize_messages"),
        ("can you summarize my texts", "summarize_messages"),
    ]:
        calls = direct(prompt)
        check(f"{prompt!r} -> {tool}", calls == [(tool, {})], f"got {calls}")

    # Each of these carries an argument the router has NOT resolved (unread /
    # period / sender), so it must fall through to the ordinary route where the
    # model extracts it.
    for prompt in ["summarize my unread emails",
                   "summarize my emails from yesterday",
                   "summarize my messages from mom",
                   "summarize my inbox for this week"]:
        calls = direct(prompt)
        check(f"{prompt!r} declines (qualifier the router didn't resolve)",
              calls == [], f"got {calls}")


def test_direct_routes_never_force_a_selection_step() -> None:
    print("\na direct route must not re-arm the selection step it exists to skip")
    for prompt in ["what's my battery level", "what's on my calendar today",
                   "summarize my inbox", "lock my screen"]:
        d = router.rule_route(prompt)
        check(f"{prompt!r}: expect_tool_first is off",
              d is not None and not d.expect_tool_first)
        check(f"{prompt!r}: force_first_tool is unset",
              d is not None and d.force_first_tool is None)
        # The narration gate requires every CALLED tool to have answered; if the
        # route offered tools it never called, nothing is lost, but offering
        # exactly the pre-called set is what makes the gate satisfiable in
        # "strict" mode too.
        check(f"{prompt!r}: offers exactly the pre-called tools",
              d is not None and d.tool_subset == [n for n, _ in d.direct_calls],
              f"subset={d.tool_subset if d else None} calls={d.direct_calls if d else None}")


def test_as_dict_is_json_shaped() -> None:
    print("\nas_dict emits direct_calls in wire shape (it is the /router/classify format)")
    d = router.rule_route("what's my battery level")
    payload = d.as_dict()
    check("direct_calls is a list of {tool, args} dicts",
          payload["direct_calls"] == [{"tool": "get_battery_status", "args": {}}],
          f"got {payload['direct_calls']}")
    other = router.rule_route("hello there")
    check("a non-direct route emits an empty list, not a missing key",
          other is not None and other.as_dict()["direct_calls"] == [])


if __name__ == "__main__":
    test_single_zero_arg_device_tool_dispatches()
    test_two_named_tools_decline()
    test_device_writes_never_dispatch_the_read()
    test_calendar_window_resolved_in_python()
    test_calendar_declines_writes_past_and_compound()
    test_summary_dispatches_only_when_unqualified()
    test_direct_routes_never_force_a_selection_step()
    test_as_dict_is_json_shaped()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
