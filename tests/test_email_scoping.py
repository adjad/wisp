"""Date-scoped email lookups must never read as "your inbox is empty".

VERIFIED FAILURE 2026-08-10 02:32, from the user's own debug export. Asked "is
there anything needed to be responded to on my emails?" — a question naming no
time at all — the model reached for `period="this week"`. At 2:32am on a MONDAY
"this week" is two and a half hours old, so it matched zero emails and the tool
answered:

    No emails found for this week.

The model reported there was nothing to respond to. There was plenty; it had all
arrived before midnight. The user pushed back ("like u mean I have no emails this
week or nothing important") and the model re-ran the identical query and said the
same thing again.

Two independent defects, both covered here:
  1. the tool's empty-range answer was a DEAD END that reads as "inbox empty";
  2. the tool DESCRIPTIONS invited a date scope on a question with no date in it
     (PERIOD_ARG literally said to use it for "lately" questions).

    .venv/bin/python tests/test_email_scoping.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.tools.email_tools as E  # noqa: E402
import service.tools.timeranges as T  # noqa: E402
from service.tools.timeranges import PERIOD_ARG  # noqa: E402

PASS, FAIL = 0, 0

# --------------------------------------------------------------------------
# A FROZEN CLOCK.
#
# Every fixture below asserts on the shape of an EMPTY date range, and "empty"
# is a claim about which side of a calendar boundary a timestamp falls on.
# Built from the real clock, that premise only holds on some days: the inbox
# rows were seeded at `time.time() - 30h`, which lands BEFORE the start of
# "this week" on a Monday and INSIDE it from Tuesday onward. So from Tuesday to
# Sunday the range was not empty, the deterministic "NOT EMPTY" scaffold was
# never produced, and the model summary came back instead — a fixture failure
# wearing the costume of a product bug. (Verified Tue 2026-08-18: 5 failures
# here against both this tree and the installed copy in /Applications.)
#
# The fix is to pin "now" rather than to hunt for an offset that happens to
# work today. It is pinned to the exact instant the real bug was captured at:
# Monday 2026-08-10 02:32, when "this week" was two and a half hours old.
NOW = datetime(2026, 8, 10, 2, 32)
assert NOW.strftime("%A") == "Monday", "the empty-range premise needs a Monday"
NOW_TS = NOW.timestamp()


class _FrozenDatetime(datetime):
    """`datetime` with a fixed `now()`; everything else is inherited."""

    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW

    @classmethod
    def today(cls):
        return NOW


def freeze_clock() -> None:
    """Rebind `datetime` inside each module that resolves "now".

    Both do `from datetime import datetime`, so the name lives in THEIR
    namespace — patching the datetime module itself would not be seen.
    `timeranges` is where `period` becomes a window (resolve_period ->
    datetime.now()); `email_tools` is where `day` does (_day_bounds).
    """
    E.datetime = _FrozenDatetime
    T.datetime = _FrozenDatetime


def unfreeze_clock() -> None:
    E.datetime = datetime
    T.datetime = datetime


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def _headers(rows: list[tuple[float, str]]) -> str:
    """rows of (ts, 'R'|'U') -> a header cache in the current wire format."""
    return "\n".join(f"{ts} | {flag} | Google | Sender{i} | Subject {i}"
                     for i, (ts, flag) in enumerate(rows))


def with_cache(text: str):
    E._headers = text
    E._history = ""


# --------------------------------------------------------------------------

def test_empty_range_says_the_inbox_is_not_empty() -> None:
    print("\nan empty range states what IS there instead of dead-ending")
    yesterday = NOW_TS - 30 * 3600               # before midnight, like the real case
    with_cache(_headers([(yesterday, "U"), (yesterday - 60, "R")]))
    out = asyncio.run(E.summarize_inbox_for_period("this week"))
    check("does not answer with a bare 'No emails found'",
          out.strip() != "No emails found for this week.", out[:80])
    check("says the inbox is NOT empty", "NOT EMPTY" in out, out[:160])
    check("reports how many emails there actually are", "2 recent emails" in out, out[:200])
    check("tells the model how to recover (drop period/day)",
          "NO `period`" in out and "NO `day`" in out, out[:240])
    check("explicitly forbids concluding there is nothing to respond to",
          "nothing to respond to" in out, out[:240])
    check("states the window it actually covered", "range covers only" in out, out[:160])


def test_empty_single_day_gets_the_same_treatment() -> None:
    print("\nthe single-day path had the same hole")
    # It used to hand an EMPTY line list to the summarizer, which then wrote
    # prose about nothing at all.
    with_cache(_headers([(NOW_TS - 30 * 3600, "U")]))
    out = asyncio.run(E.summarize_inbox_for_day("today"))
    check("today with no mail today still says the inbox isn't empty",
          "NOT EMPTY" in out, out[:160])


def test_genuinely_empty_inbox_stays_honest() -> None:
    print("\na genuinely empty cache must NOT claim there are emails")
    with_cache("")
    out = asyncio.run(E.summarize_inbox_for_period("this week"))
    check("no false 'NOT EMPTY' claim", "NOT EMPTY" not in out, out[:120])


def test_period_arg_no_longer_invites_a_date_on_undated_questions() -> None:
    print("\nPERIOD_ARG stops inviting a scope the user never asked for")
    d = PERIOD_ARG["description"]
    check("no longer advertises itself for 'lately'", "lately" not in d, d[:120])
    check("says ONLY when the user named a time", "ONLY pass this when the user NAMED" in d)
    check("names the undated questions that must omit it",
          "anything I need to reply to?" in d, d[-260:])
    check("warns that a calendar range can be near-empty",
          "few hours long" in d, d[-200:])


# --------------------------------------------------------------------------
# Unread — read status was never captured at all, so "what's unread" was
# structurally unanswerable (Wisp could only WRITE it, via mark_email_read).

def test_read_flag_parses_in_both_formats() -> None:
    print("\nheader lines parse with the read flag and without it")
    E._headers = "\n".join([
        "1786329515.0 | U | Google | Alice | Unread one",
        "1786329500.0 | R | Google | Bob | Read one",
        "1786329400.0 | U | UCSC | Carol | Subject with | a pipe in it",
        "1786329300.0 | Google | Dave | Old-format line",
        "1786329200.0 | Google | Eve | Old format | with a pipe",
    ])
    rows = E._parse_lines()
    check("all five lines parsed", len(rows) == 5, f"{len(rows)}")
    check("U -> unread True", rows[0][4] is True)
    check("R -> unread False", rows[1][4] is False)
    check("a subject containing ' | ' survives the flagged format",
          rows[2][3] == "Subject with | a pipe in it", rows[2][3])
    check("old-format line -> unread None (unknown, NOT read)", rows[3][4] is None)
    check("old-format line with a pipe keeps its whole subject",
          rows[4][3] == "Old format | with a pipe", rows[4][3])


def test_unknown_read_status_never_claims_zero_unread() -> None:
    print("\nunknown read status is reported, never silently treated as 'read'")
    # The caches are restored from disk across restarts, so right after an
    # upgrade every cached line predates the flag. Answering "you have no
    # unread mail" from records that never knew would be confidently wrong.
    E._headers = "1786329300.0 | Google | Dave | Old-format line"
    rows, note = E._unread_rows(E._parse_lines())
    check("no rows returned", rows == [])
    check("and it SAYS status isn't available yet",
          "isn't in the mail cache yet" in note, note)
    check("tells the model not to claim there are none",
          "rather than saying they have none" in note, note)


def test_unread_filter_selects_only_unread() -> None:
    print("\nthe unread filter keeps unread rows and flags partial coverage")
    E._headers = "\n".join([
        "1786329515.0 | U | Google | Alice | Unread one",
        "1786329500.0 | R | Google | Bob | Read one",
        "1786329300.0 | Google | Dave | Pre-flag line",
    ])
    rows, note = E._unread_rows(E._parse_lines())
    check("only the unread row survives",
          [r[2] for r in rows] == ["Alice"], [r[2] for r in rows])
    check("the pre-flag row is disclosed rather than dropped silently",
          "1 older cached emails" in note, note)


def test_unread_is_an_exposed_tool_argument() -> None:
    print("\nboth email tools actually expose `unread`")
    from service.tools import get_tool
    import service.tools  # noqa: F401  (ensures registration)
    for name in ("summarize_emails", "view_emails"):
        t = get_tool(name)
        check(f"{name} takes unread", "unread" in t.parameters["properties"],
              str(list(t.parameters["properties"])))


if __name__ == "__main__":
    freeze_clock()
    try:
        test_empty_range_says_the_inbox_is_not_empty()
        test_empty_single_day_gets_the_same_treatment()
        test_genuinely_empty_inbox_stays_honest()
        test_period_arg_no_longer_invites_a_date_on_undated_questions()
        test_read_flag_parses_in_both_formats()
        test_unknown_read_status_never_claims_zero_unread()
        test_unread_filter_selects_only_unread()
        test_unread_is_an_exposed_tool_argument()
    finally:
        E._headers = ""
        unfreeze_clock()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
