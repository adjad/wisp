"""Capability Atlas Phase 1 (T1) — the tools, and the routes that must reach them.

Two failure modes are covered here, and the second is the one that actually bit.

  THE TOOLS THEMSELVES. Duration parsing, arithmetic, unit conversion and time
  zones are all pure functions with exact right answers, so they are tested as
  such. `calculate` gets extra attention because it evaluates model-authored
  text: it must refuse anything that is not arithmetic, at parse time.

  THE ROUTES THAT REACH THEM. Registering a tool is not the same as making it
  reachable. A request claimed by an existing regex route gets that route's
  hardcoded subset, which cannot contain a tool written after it — so
  `find_files` shipped invisible to "find the file with the lease in it", and
  `create_note` invisible to "take a note". Both are asserted here because both
  happened, and neither showed up as a test failure or an error — just the old,
  worse behavior continuing silently.

Runs offline. No oMLX, no network: the routing checks that need retrieval stub
the embedder the same way tests/test_semantic_routing.py does.

    .venv/bin/python tests/test_phase1_tools.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.tools  # noqa: E402,F401  (registers every tool module)
from service.tools.conversions import calculate, convert_units  # noqa: E402
from service.tools.reference_tools import world_time  # noqa: E402
from service.tools.timers_alarms import _parse_duration  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def test_duration_parsing() -> None:
    print("\ntimer durations")
    for text, want in [("10 minutes", 600), ("1h30m", 5400), ("90s", 90),
                       ("10", 600), ("2 hrs", 7200), ("1 hour 15 minutes", 4500),
                       ("3h", 10800), ("45s", 45)]:
        got = _parse_duration(text)
        check(f"{text!r} -> {want}s", got == want, f"got {got}")
    # "1h30m" needs the unit lookahead to NOT be \b (h is followed by a digit),
    # and "90s" needs "s" spelled out in the table ("s".rstrip("s") == "").
    # Both of those were live bugs; the cases above are their regression.
    for junk in ["bananas", "monday", "sunday brunch", ""]:
        check(f"{junk!r} is refused", _parse_duration(junk) is None)


def test_calculate() -> None:
    print("\narithmetic is computed, not generated")
    for expr, want in [("20% of 250", "50"), ("(120+35)*3", "465"),
                       ("87/4", "21.75"), ("2**10", "1,024"),
                       ("15 times 32", "480"), ("100 divided by 4", "25"),
                       ("18% of 64.50", "11.61")]:
        got = calculate(expr)
        check(f"{expr!r} = {want}", got == want, f"got {got!r}")

    print("\n  calculate refuses everything that isn't arithmetic")
    # It evaluates model-authored text, so this is the security boundary, not a
    # niceness check. Each of these must be refused at PARSE time.
    for hostile in ['__import__("os").system("echo pwned")',
                    'open("/etc/passwd")',
                    "().__class__.__bases__",
                    "[x for x in range(10)]",
                    "lambda: 1"]:
        got = calculate(hostile)
        check(f"refuses {hostile[:34]!r}", got.startswith("(error"), f"got {got!r}")
    for bad, why in [("1/0", "division by zero"), ("9**9**9", "too large")]:
        got = calculate(bad)
        check(f"{bad!r} -> {why}", got.startswith("(error"), f"got {got!r}")


def test_conversions() -> None:
    print("\nunit conversion")
    checks = [
        (180, "lb", "kg", "81.6"), (26.2, "miles", "km", "42.1"),
        (350, "F", "C", "176.6"), (30, "C", "F", "86"), (5, "GB", "MB", "5,000"),
    ]
    for value, src, dst, expect in checks:
        got = convert_units(value, src, dst)
        check(f"{value}{src} -> {dst} contains {expect}", expect in got, f"got {got!r}")
    check("mismatched dimensions are refused",
          convert_units(1, "kg", "miles").startswith("(error"))
    check("unknown units are refused",
          convert_units(3, "flurbs", "kg").startswith("(error"))


def test_world_time() -> None:
    print("\nthe clock")
    out = world_time()
    check("local time answers", ":" in out and "It's" in out, out)
    check("an unknown place is refused",
          world_time("Narnia").startswith("(error"))
    # India is UTC+5:30 — a half-hour zone, which is where naive integer-offset
    # implementations go wrong.
    out = world_time("kolkata")
    check("half-hour zones are handled", "Kolkata" in out, out)
    # Abbreviations resolve to the zone's real city name, not "Sf".
    out = world_time("sf", "sydney")
    check("'sf' labels as Los Angeles", "Los Angeles" in out, out)
    check("comparison names both places", "Sydney" in out, out)


async def test_routes_reach_the_new_tools() -> None:
    print("\nthe routes actually reach them")
    # Retrieval is stubbed: this asserts ROUTING, and must not need oMLX.
    from tests.stub_embedder import install
    install()

    from service.router.router import route

    async def subset(prompt: str) -> list[str]:
        d = await route(prompt)
        return d.tool_subset or []

    # find_files: the regex document route has its own hardcoded subset, so
    # adding the tool to the registry was NOT enough to reach this phrasing.
    s = await subset("find the file with the lease in it")
    check("a file-search request reaches find_files", "find_files" in s, f"-> {s}")

    # create_note: the notes route offered only search_notes, and "take a note"
    # contains none of _WRITE_INTENT_RE's verbs.
    s = await subset("take a note about the wifi password")
    check("'take a note' reaches create_note", "create_note" in s, f"-> {s}")
    s = await subset("what did I write in my notes")
    check("a notes READ stays read-only", "create_note" not in s, f"-> {s}")

    # A PIN is not a program — CODE_RE used to claim this for the tool-less
    # coding model.
    d = await route("jot down that the door code is 1234")
    check("'door code' is not a coding request", d.role != "coding", f"role={d.role}")
    d = await route("write a python function to sort a list")
    check("real code requests still route to coding", d.role == "coding", f"role={d.role}")

    # The unscoped action route used to hand over the ENTIRE registry.
    d = await route("set a timer for 10 minutes")
    s = d.tool_subset or []
    check("a timer request is scoped, not unscoped", bool(s), "still unscoped")
    check("…and reaches set_timer", "set_timer" in s, f"-> {s}")

    # window_control had the same problem as find_files: the apps/media route
    # claims "close this window", so registering the tool wasn't enough.
    s = await subset("close this window")
    check("'close this window' reaches window_control", "window_control" in s, f"-> {s}")

    # NO route may hand over the whole registry any more. This is the invariant
    # that has to hold as the Capability Atlas grows the registry to ~198: at
    # that size an unscoped route is ~42,000 tokens and simply cannot run.
    from service.tools.registry import REGISTRY
    for prompt in ("what do I have open", "set a timer for 10 minutes",
                   "take a screenshot", "archive those emails",
                   "what apps are running"):
        d = await route(prompt)
        if not d.needs_tools:
            continue
        n = len(d.tool_subset or [])
        check(f"{prompt[:28]!r} is scoped ({n} tools)",
              0 < n < len(REGISTRY), f"{n} of {len(REGISTRY)}")

    # A bare confirmation with no domain to inherit retrieves against the
    # ASSISTANT'S offer — "yes" itself carries no signal, and the whole registry
    # is the measured-worst menu at the moment the user has already said do it.
    # The offer has to be phrased as one for `confirms_offered_action` to fire —
    # a bare "Archive them?" is not detected as an offer today, only forms like
    # "Want me to …?".
    #
    # Asserted STRUCTURALLY (scoped, non-empty, retrieved from the offer) rather
    # than "archive_email is in there": the embedder is stubbed here with a
    # word-overlap stand-in, so which tool lands in the top 10 is a property of
    # the stub, not of this code. The semantic version of this check belongs in
    # scripts/test_retrieval.py, which runs against the real model.
    d = await route("yes go ahead",
                    last_assistant="I found 3 emails from the landlord. "
                                   "Want me to archive them?")
    s = d.tool_subset or []
    check("a bare confirmation is scoped, not the whole registry",
          0 < len(s) < len(REGISTRY), f"{len(s)} of {len(REGISTRY)}")
    check("…and still carries the escape hatch", "run_shell" in s, f"-> {s}")

    # Completing is not cancelling. The calendar write route offered only
    # cancel_event, which DELETES — the destructive reading of a request that
    # wasn't destructive, and not recoverable.
    s = await subset("mark the dentist reminder as done")
    check("'mark as done' reaches complete_reminder", "complete_reminder" in s, f"-> {s}")

    # An availability question is not a schedule read-out. This one was
    # direct-dispatched to get_upcoming, leaving the model to eyeball gaps out
    # of a list of appointments.
    d = await route("when am I free this week")
    s = d.tool_subset or []
    check("'when am I free' is not direct-dispatched", not d.direct_calls,
          f"direct={[n for n, _ in d.direct_calls]}")
    check("…and reaches find_free_time", "find_free_time" in s, f"-> {s}")
    # …while a plain read keeps its fast path, which is the point of having one.
    d = await route("what's on my calendar today")
    check("a plain schedule read still direct-dispatches",
          [n for n, _ in d.direct_calls] == ["get_upcoming"],
          f"direct={[n for n, _ in d.direct_calls]}")


def main() -> int:
    test_duration_parsing()
    test_calculate()
    test_conversions()
    test_world_time()
    asyncio.run(test_routes_reach_the_new_tools())
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
