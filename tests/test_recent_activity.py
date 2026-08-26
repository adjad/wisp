"""The cross-app recency feed, its routing, and the two follow-on router fixes.

`get_recent_activity` answers "what's new / anything I missed" from every source
at once — messages, mail, newly-added reminders/events, notes — in one cheap
NO-MODEL read, instead of the model calling three or four tools and merging them
itself (slow, and it usually stopped at the first source that answered).

Also covered here, both from real 2026-08-10 failures:
  * "any messages from mom" was widened to the EMAIL tools as well, because
    _INBOUND_RE fires on "any messages from" and the widening ignored that a
    channel had been named. Messages means Messages.app.
  * "set some more the day before it", right after two add_reminder calls, fell
    through to the ambiguous core set — which contains no add_reminder — and the
    model answered "I don't have the ability to create new calendar events or
    reminders with my available tools." It does; it just wasn't offered them.

    .venv/bin/python tests/test_recent_activity.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.skills import load as load_skills  # noqa: E402

load_skills()

import service.router.router as R  # noqa: E402
import service.tools.email_tools as E  # noqa: E402
import service.tools.imessage_tools as M  # noqa: E402
import service.tools.notes_tools as N  # noqa: E402
import service.tools.recent_tools as RT  # noqa: E402
from service.assistant.store import assistant_store  # noqa: E402
from service.router.router import route  # noqa: E402

PASS, FAIL = 0, 0

# --------------------------------------------------------------------------
# A FROZEN CLOCK, AND FIXTURES THAT OWN EVERY SOURCE.
#
# `get_recent_activity` asks each source "what have you got since T?", so a
# fixture claiming a window is EMPTY is only true if it controls every source
# feeding that window. This one controlled two of four: `seed()` stubbed
# Messages and Mail, but the commitment store and Notes were read straight off
# the developer's own machine. Verified Tue 2026-08-18: the "nothing in the
# last hour, so widen" case failed because a REAL calendar row —
#
#     [ 35 min ago]  CALENDAR  added: UCSC Move-In Appointment
#
# had been added 35 minutes earlier, so the 1-hour window was not empty and the
# widening never fired. Nothing to do with the code under test; the test simply
# passed or failed on whether the machine had been quiet for an hour.
#
# So: pin "now" (rather than picking offsets that happen to work today), and
# route all four sources through the fixture. `test_truly_quiet_is_reported_
# honestly` used to carry a skip branch for exactly this leak — with the
# sources owned, it can assert unconditionally.
NOW_DT = datetime(2026, 8, 10, 2, 32)          # the instant the real bug was captured
NOW = NOW_DT.timestamp()

_REAL_RECENTLY_ADDED = assistant_store.recently_added
_REAL_NOTES_PARSE = N._parse

_COMMITMENTS: list[dict] = []
_NOTES: list[dict] = []


@contextlib.contextmanager
def _frozen_clock():
    """Pin `time.time()` for the duration of one feed render.

    Scoped to the render rather than the whole module on purpose: the routing
    tests below call the real router, and there is no reason to run that under
    a clock it never sees in production.
    """
    real = time.time
    time.time = lambda: NOW
    try:
        yield
    finally:
        time.time = real


def isolate_local_sources() -> None:
    """Point the commitment store and Notes at the fixture lists.

    `_collect` imports both lazily inside its own try blocks, so patching the
    singleton and the module attribute is enough — it picks up whatever is
    bound at call time.
    """
    assistant_store.recently_added = (
        lambda since_ts, limit=20: [r for r in _COMMITMENTS
                                    if float(r.get("created_at") or 0) >= since_ts][:limit])
    N._parse = lambda: list(_NOTES)


def restore_local_sources() -> None:
    assistant_store.recently_added = _REAL_RECENTLY_ADDED
    N._parse = _REAL_NOTES_PARSE


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def dec(prompt: str, **kw):
    return asyncio.run(route(prompt, **kw))


def seed(*, msgs=(), emails=(), commitments=(), notes=()):
    M._lines = "\n".join(f"{ts} | {ctx} | {txt}" for ts, ctx, txt in msgs)
    E._headers = "\n".join(f"{ts} | {flag} | Google | {sender} | {subj}"
                           for ts, flag, sender, subj in emails)
    E._history = ""
    _COMMITMENTS[:] = list(commitments)
    _NOTES[:] = list(notes)


def render(**kw) -> str:
    with _frozen_clock():
        return asyncio.run(RT.get_recent_activity(**kw))


# --------------------------------------------------------------------------

def test_merges_every_source_newest_first() -> None:
    print("\nsources are merged into one recency-ordered feed")
    seed(msgs=[(NOW - 600, "Mom", "are you coming sunday?")],
         emails=[(NOW - 3600, "U", "Indeed", "Sales Associate role")])
    out = render()
    check("messages appear", "MESSAGES" in out and "Mom" in out, out[:200])
    check("email appears", "EMAIL" in out and "Indeed" in out, out[:200])
    check("the newer item is listed first",
          out.index("MESSAGES") < out.index("EMAIL"), out[:200])
    check("known-unread mail is marked", "(unread)" in out, out[:200])


def test_one_line_per_conversation_not_per_message() -> None:
    print("\na chatty thread collapses to one line with a count")
    # Verified on real data: a single group thread produced 12+ lines and the
    # 7-day feed contained no email at all — the opposite of a cross-app view.
    seed(msgs=[(NOW - 60 * i, "Group \"Comp\"", f"msg {i}") for i in range(1, 16)],
         emails=[(NOW - 3600, "R", "Indeed", "A job")])
    out = render()
    check("the thread contributes exactly one line",
          out.count('Group "Comp"') == 1, f"{out.count(chr(34))} / {out[:200]}")
    check("and discloses how many it stands for", "(+14 more)" in out, out[:240])
    check("email still gets through", "EMAIL" in out, out[:240])


def test_empty_window_widens_instead_of_dead_ending() -> None:
    print("\nan empty window widens rather than reporting 'nothing new'")
    # Same class of bug as summarize_emails' "No emails found for this week."
    seed(msgs=[(NOW - 50 * 3600, "Mom", "older message")])
    out = render(hours=1)
    check("it returned something rather than 'nothing new'",
          "Nothing new turned up" not in out, out[:200])
    check("and says the ASKED-FOR window was empty, naming it correctly",
          "Nothing in the last hour, so this covers" in out, out[:160])
    check("telling the model not to report nothing happened",
          "rather than reporting that nothing has happened" in out, out[:200])


def test_truly_quiet_is_reported_honestly() -> None:
    print("\nnothing anywhere -> an honest 'quiet', with a syncing caveat")
    # Every source is the fixture's now, so "nothing anywhere" is a fact rather
    # than a hope about the machine — this no longer skips itself when the
    # developer happens to have added a reminder recently.
    seed()
    out = render()
    check("says everything is quiet", "quiet" in out, out[:160])
    check("mentions caches may still be syncing", "syncing" in out, out[:200])


def test_no_model_call_and_fast() -> None:
    print("\nit is a pure data read — no model call")
    seed(msgs=[(NOW - 600, "Mom", "hi")])
    t0 = time.monotonic()
    render()
    check(f"returns in well under a second ({time.monotonic() - t0:.3f}s)",
          time.monotonic() - t0 < 1.0)


def test_notes_is_quota_limited() -> None:
    print("\nnotes can never flood the feed (weakest source for this user)")
    check("notes quota is small and smaller than the message cap",
          RT._NOTES_QUOTA <= 3 and RT._NOTES_QUOTA < RT._CAP_MESSAGES,
          f"notes={RT._NOTES_QUOTA} msgs={RT._CAP_MESSAGES}")


def test_recency_questions_route_to_it() -> None:
    print("\n'what's new'-shaped questions route to the feed and force it")
    for prompt in ("what's new", "anything new?", "catch me up", "any updates",
                   "did i miss anything", "what's the latest",
                   "anything happen while I was out"):
        d = dec(prompt)
        check(f"{prompt!r} forces get_recent_activity",
              d.force_first_tool == "get_recent_activity", f"-> {d.reason}")


def test_todo_questions_are_not_stolen() -> None:
    print("\n…but to-do/planning questions keep the aggregate route")
    # These look similar and mean the opposite: outstanding vs changed.
    for prompt in ("what do I need to do", "help me plan my day",
                   "anything i need to do", "what's on my todo list"):
        d = dec(prompt)
        check(f"{prompt!r} stays on the 4-source aggregate",
              d.force_first_tool != "get_recent_activity"
              and set(d.tool_subset or []) >= set(R._ALL_SOURCES), f"-> {d.reason}")


def test_named_channel_is_not_widened() -> None:
    print("\n'messages' means Messages.app — never widened to email")
    s = dec("any messages from mom").tool_subset or []
    check("messages only", any("messages" in t for t in s) and not any("email" in t for t in s), f"-> {s}")
    s = dec("any new texts from trishe").tool_subset or []
    check("texts -> messages only", not any("email" in t for t in s), f"-> {s}")
    s = dec("any emails from mom").tool_subset or []
    check("email only, symmetrically",
          any("email" in t for t in s) and not any("messages" in t for t in s), f"-> {s}")
    print("  …and a request naming NO channel still checks both")
    for prompt in ("did i hear back from dan", "did i get anything from the school"):
        s = dec(prompt).tool_subset or []
        check(f"{prompt!r} covers both",
              any("email" in t for t in s) and any("messages" in t for t in s), f"-> {s}")


def test_write_continuation_inherits_the_write_tools() -> None:
    print("\na follow-up that extends the previous WRITE gets that domain's tools")
    for prompt in ("set some more the day before it", "add another one",
                   "one more for friday", "also set one for tuesday",
                   "do the same for friday"):
        s = dec(prompt, last_tools="add_reminder,add_reminder").tool_subset or []
        check(f"{prompt!r} can actually create one", "add_reminder" in s, f"-> {s}")

    print("  …but not without a prior write to continue")
    for prompt in ("set some more the day before it", "add another one"):
        d = dec(prompt, last_tools=None)
        check(f"{prompt!r} falls back normally",
              "continues previous" not in d.reason, d.reason)

    print("  …and it never hijacks a self-contained request")
    for prompt in ("remind me to call mom at 5", "what's on my calendar",
                   "set a reminder for tuesday at 9"):
        d = dec(prompt, last_tools="add_reminder")
        check(f"{prompt!r} keeps its own route",
              "continues previous" not in d.reason, d.reason)

    print("  …and it does not force a tool call (the target is often ambiguous)")
    d = dec("set some more the day before it", last_tools="add_reminder")
    check("expect_tool_first stays False", d.expect_tool_first is False)


if __name__ == "__main__":
    isolate_local_sources()
    try:
        test_merges_every_source_newest_first()
        test_one_line_per_conversation_not_per_message()
        test_empty_window_widens_instead_of_dead_ending()
        test_truly_quiet_is_reported_honestly()
        test_no_model_call_and_fast()
        test_notes_is_quota_limited()
        test_recency_questions_route_to_it()
        test_todo_questions_are_not_stolen()
        test_named_channel_is_not_widened()
        test_write_continuation_inherits_the_write_tools()
    finally:
        seed()
        restore_local_sources()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
