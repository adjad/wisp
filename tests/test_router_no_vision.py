"""Routing after the vision path was removed — regression tests.

Wisp no longer accepts images as a prompt source and has no vision model, so
`see_screen`/`describe_image`, the `vision` role, and `has_image` threading were
all removed on 2026-08-08.

Two things that must keep holding, and one bug the removal EXPOSED:

  * The screen phrasings ("what's on my screen", "describe my screen", "what am
    I looking at") used to live in STRONG_ACTION_RE purely so they'd reach
    see_screen. They're gone. These prompts must now fall through the ordinary
    rule chain — and in particular must NOT be claimed by the calendar route,
    which has nothing to say about a screen.

  * STRONG_ACTION_RE was doing double duty. Because it matched those phrasings
    first, `_domain_subset` bailed out at its machine-action guard before any
    calendar rule ran — which silently shielded an over-broad branch in
    SCHEDULE_RE:

        r"what'?s\\s+(?:on|due|coming up|next|happening|scheduled)\\b"

    That bare `what's on` matches "what's on my screen", "what's on my
    clipboard", "what's on TV". `_CALENDAR_READ_RE` had already learned this
    lesson and anchors its own `what's on/next` to a calendar noun, with a
    comment naming these exact three examples; SCHEDULE_RE never did. Removing
    the shield made it real: "what's on my screen" started routing to a
    get_upcoming-only calendar lookup. SCHEDULE_RE is now anchored the same way.

  * The anchoring must not cost any genuine calendar read. The bare
    calendar-specific predicates (due / coming up / happening / scheduled) stay
    unanchored, because those are unambiguous.

    .venv/bin/python tests/test_router_no_vision.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.router.router import route  # noqa: E402
from service.tools import tool_schemas  # noqa: E402
from service.config import _packaged_config  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def subset_for(prompt: str) -> list[str]:
    """The tool subset a prompt routes to ([] means the full toolset)."""
    return asyncio.run(route(prompt)).tool_subset or []


# --------------------------------------------------------------------------

def test_vision_tools_are_gone() -> None:
    print("\nthe vision tools are no longer registered")
    names = {s["function"]["name"] for s in tool_schemas(None)}
    check("see_screen is not registered", "see_screen" not in names)
    check("describe_image is not registered", "describe_image" not in names)


def test_model_roster_exposes_no_vision_role() -> None:
    print("\nthe packaged model roster exposes no vision route")
    roles = _packaged_config()["roles"]
    vision_roles = [role for role in roles if role.casefold() == "vision"]
    check("no vision role is configured", not vision_roles, repr(vision_roles))


def test_testing_guide_does_not_promise_removed_screen_access() -> None:
    print("\nthe active QA guide does not promise a removed vision tool")
    guide = (Path(__file__).resolve().parents[1] / "TESTING.md").read_text(encoding="utf-8")
    check("see_screen is not promised", "routes to\n  `see_screen`" not in guide)
    check("honest no-screen behavior is documented", "no vision tool is offered" in guide)


def test_route_takes_no_image() -> None:
    print("\nroute() no longer accepts an image")
    try:
        asyncio.run(route("hello", has_image=True))  # type: ignore[call-arg]
        check("route(has_image=...) is rejected", False, "it was accepted")
    except TypeError:
        check("route(has_image=...) is rejected", True)


def test_screen_prompts_are_not_calendar() -> None:
    print("\nscreen phrasings must not be claimed by the calendar route")
    # The three examples _CALENDAR_READ_RE's own comment warns about, plus the
    # phrasings STRONG_ACTION_RE used to carry.
    #
    # Checks the ROUTE, not tool membership. This used to assert
    # "get_upcoming" not in the subset, as a proxy for "wasn't routed to
    # calendar". That proxy stopped being valid once the ambiguous fallback got
    # its own core subset: the core legitimately includes get_upcoming as one of
    # the four read sources, so these prompts now contain it while being
    # correctly routed. The narrow calendar route is what must not claim them.
    for prompt in ("what's on my screen",
                   "what's on my clipboard",
                   "what's on TV tonight",
                   "describe my screen",
                   "what am I looking at"):
        d = asyncio.run(route(prompt))
        sub = d.tool_subset or []
        calendar_only = bool(sub) and set(sub) <= {"get_upcoming", "get_past_events"}
        check(f"{prompt!r} is not a calendar lookup",
              not calendar_only and "calendar" not in d.reason.lower(),
              f"-> {d.reason} :: {sub}")


def test_real_calendar_reads_still_route() -> None:
    print("\ngenuine calendar reads are unaffected by the anchoring")
    for prompt in ("what's on my calendar",
                   "what's on my calendar today",
                   "what's on today",
                   "what's on my agenda",
                   "what's next on my schedule",
                   "what's due this week",
                   "what's coming up",
                   "what's happening tomorrow",
                   "am I free friday",
                   "do I have any meetings today"):
        check(f"{prompt!r} still reaches get_upcoming",
              "get_upcoming" in subset_for(prompt),
              f"-> {subset_for(prompt) or 'ALL'}")


def test_bare_scheduled_is_a_calendar_read() -> None:
    print("\n'what's scheduled' is a calendar question, not a send-queue one")
    # _SCHEDULED_QUERY_RE runs before every other domain rule in
    # _domain_subset, so an unanchored "what…scheduled" branch beat
    # SCHEDULE_RE's own bare "scheduled" calendar predicate on ordering alone
    # and answered "what's scheduled today" with schedule_send /
    # list_scheduled_sends / cancel_scheduled_send.
    for prompt in ("what's scheduled",
                   "what's scheduled today",
                   "what's scheduled this week"):
        sub = subset_for(prompt)
        check(f"{prompt!r} reaches get_upcoming", "get_upcoming" in sub,
              f"-> {sub or 'ALL'}")
        check(f"{prompt!r} is not the send queue",
              "list_scheduled_sends" not in sub, f"-> {sub or 'ALL'}")
    # No calendar read rule matches this phrasing ("what <noun> are scheduled"
    # is covered by neither _CALENDAR_READ_RE nor SCHEDULE_RE), so it lands on
    # the full toolset — which still contains get_upcoming. Unscoped is a
    # separate, pre-existing gap; what matters here is that it is no longer
    # actively misrouted to the send queue.
    sub = subset_for("what meetings are scheduled tomorrow")
    check("'what meetings are scheduled tomorrow' is not the send queue",
          "list_scheduled_sends" not in sub, f"-> {sub or 'ALL'}")


def test_send_queue_queries_still_route() -> None:
    print("\ngenuine send-queue questions still reach the queue tools")
    for prompt in ("what's scheduled to send",
                   "what emails are scheduled",
                   "what texts are queued",
                   "anything queued to go out later",
                   "show me my scheduled sends",
                   "cancel that scheduled email"):
        sub = subset_for(prompt)
        check(f"{prompt!r} reaches the send queue",
              "list_scheduled_sends" in sub or "cancel_scheduled_send" in sub,
              f"-> {sub or 'ALL'}")


def test_document_route_drops_describe_image() -> None:
    print("\nthe file/document route no longer offers a tool that doesn't exist")
    sub = subset_for("summarize the PDF in my downloads")
    check("describe_image is not offered", "describe_image" not in sub)
    check("read_file still is", "read_file" in sub, f"-> {sub}")


if __name__ == "__main__":
    test_vision_tools_are_gone()
    test_model_roster_exposes_no_vision_role()
    test_testing_guide_does_not_promise_removed_screen_access()
    test_route_takes_no_image()
    test_screen_prompts_are_not_calendar()
    test_real_calendar_reads_still_route()
    test_bare_scheduled_is_a_calendar_read()
    test_send_queue_queries_still_route()
    test_document_route_drops_describe_image()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
