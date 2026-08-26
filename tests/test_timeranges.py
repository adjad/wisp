"""Spoken time ranges -> epoch windows. Regression tests.

The gap these close, from a real session (2026-08-09): asked for "what I did
this month and last month", the model had no range parameter at all — only
`day`, `count`, and `query` (a keyword search over message TEXT). So it searched
for the word "August", and got back a message from **April 9th** that happened
to contain "beginning of August". The user's summary was built from the wrong
year.

Wisp resolves these, never the model: the agent prompt already documents that
this model is "reliably wrong" at date arithmetic, so handing it `since`/`until`
ISO dates would delegate exactly the job it fails at — and an off-by-one month
boundary is invisible in the output.

    .venv/bin/python tests/test_timeranges.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.tools.timeranges import (  # noqa: E402
    BadPeriod, BadWhen, resolve_period, resolve_span, resolve_when)

PASS, FAIL = 0, 0

# Sunday, 9 August 2026 — the date of the reported failure.
NOW = datetime(2026, 8, 9, 12, 30)

# Wednesday, 19 August 2026, 21:43 — the schedule_send failure this closes:
# asked to send "in 10 minutes" at this real time, the model computed
# when_iso as 09:52 the same day (a 12-hour AM/PM error) instead of 21:52.
SEND_NOW = datetime(2026, 8, 19, 21, 43)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def span(period: str):
    s, e, label = resolve_span(period, now=NOW)
    return datetime.fromtimestamp(s), datetime.fromtimestamp(e), label


def check_window(period: str, start: str, end: str, label: str | None = None) -> None:
    s, e, lab = span(period)
    got = (s.strftime("%Y-%m-%d %H:%M"), e.strftime("%Y-%m-%d %H:%M"))
    want = (start, end)
    check(f"{period!r} -> [{start}, {end})", got == want, f"got {got}")
    if label is not None:
        check(f"{period!r} label is {label!r}", lab == label, f"got {lab!r}")


# --------------------------------------------------------------------------

def test_the_reported_case() -> None:
    print("\nthe case that failed: 'this month and last month'")
    # August 2026 back through the start of July 2026 — NOT August 2025.
    check_window("this month and last month",
                 "2026-07-01 00:00", "2026-09-01 00:00",
                 "July 2026 through August 2026")
    s, e, _ = span("this month and last month")
    apr9 = datetime(2026, 4, 9, 16, 8)          # the message it wrongly returned
    check("the April message that broke it falls OUTSIDE the window",
          not (s <= apr9 < e))
    aug2025 = datetime(2025, 8, 15)
    check("August of LAST YEAR is outside the window too",
          not (s <= aug2025 < e))


def test_months() -> None:
    print("\nmonths")
    check_window("this month", "2026-08-01 00:00", "2026-09-01 00:00", "August 2026")
    check_window("last month", "2026-07-01 00:00", "2026-08-01 00:00", "July 2026")
    check_window("2026-07", "2026-07-01 00:00", "2026-08-01 00:00", "July 2026")
    check_window("2025-12", "2025-12-01 00:00", "2026-01-01 00:00", "December 2025")


def test_month_boundaries_roll_the_year() -> None:
    print("\n'last month' in January rolls back a year")
    jan = datetime(2026, 1, 14, 9, 0)
    s, e, lab = resolve_period("last month", now=jan)
    check("Jan 2026 -> December 2025",
          datetime.fromtimestamp(s).strftime("%Y-%m") == "2025-12"
          and datetime.fromtimestamp(e).strftime("%Y-%m") == "2026-01",
          f"got {lab}")
    s, e, _ = resolve_period("this month", now=datetime(2026, 12, 3))
    check("'this month' in December ends 1 Jan next year",
          datetime.fromtimestamp(e).strftime("%Y-%m-%d") == "2027-01-01")


def test_weeks_days_years() -> None:
    print("\nweeks, days, years")
    # 2026-08-09 is a Sunday; weeks start Monday, so 'this week' began Aug 3.
    check_window("this week", "2026-08-03 00:00", "2026-08-10 00:00", "this week")
    check_window("last week", "2026-07-27 00:00", "2026-08-03 00:00", "last week")
    check_window("today", "2026-08-09 00:00", "2026-08-10 00:00", "today")
    check_window("yesterday", "2026-08-08 00:00", "2026-08-09 00:00", "yesterday")
    check_window("last 7 days", "2026-08-03 00:00", "2026-08-10 00:00")
    check_window("past 30 days", "2026-07-11 00:00", "2026-08-10 00:00")
    check_window("this year", "2026-01-01 00:00", "2027-01-01 00:00", "2026")
    check_window("last year", "2025-01-01 00:00", "2026-01-01 00:00", "2025")
    check_window("2026-08-06", "2026-08-06 00:00", "2026-08-07 00:00")


def test_new_units() -> None:
    """weeks/months/quarters/years — extended 2026-08-23. Motivated by
    get_stock_price's SEPARATE period parser silently substituting a 3-MONTH
    bucket for "exactly three weeks ago" (no matching bucket existed, so it
    guessed the nearest one and the model relabeled the result as 3-weeks-old
    in a message it actually sent). This module never guessed buckets for
    days; the fix generalizes that same exact-math discipline to every unit
    a person actually says a count of."""
    print("\nweeks/months/quarters/years, and spelled-out counts")

    # "3 weeks" is EXACTLY "21 days" — same window, not a nearest bucket.
    a = span("3 weeks")
    b = span("21 days")
    check("'3 weeks' == '21 days' exactly", a[:2] == b[:2], f"{a[:2]} vs {b[:2]}")
    check_window("3 weeks", "2026-07-20 00:00", "2026-08-10 00:00",
                 "the last 3 weeks")
    check_window("1 week", "2026-08-03 00:00", "2026-08-10 00:00",
                 "the last 1 week")

    # Spelled-out numbers match their digit equivalents.
    for phrase in ("three weeks", "last three weeks", "past three weeks"):
        c = span(phrase)
        check(f"{phrase!r} == '3 weeks'", c[:2] == a[:2], f"got {c[:2]}")

    # Calendar-exact months (not a 30-day approximation).
    check_window("1 month", "2026-07-10 00:00", "2026-08-10 00:00",
                 "the last 1 month")
    check_window("6 months", "2026-02-10 00:00", "2026-08-10 00:00",
                 "the last 6 months")

    # A quarter is exactly 3 months — same window either way.
    q = span("2 quarters")
    m6 = span("6 months")
    check("'2 quarters' == '6 months'", q[:2] == m6[:2], f"{q[:2]} vs {m6[:2]}")

    # Calendar-exact years. Start is N years before the exclusive END
    # (today + 1 day, i.e. 2026-08-10), not before `today` itself — same
    # "start = end - N units" rule that makes "last 7 days" land on
    # [today-6, today] rather than needing a separate off-by-one per unit.
    check_window("1 year", "2025-08-10 00:00", "2026-08-10 00:00",
                 "the last 1 year")
    check_window("3 years", "2023-08-10 00:00", "2026-08-10 00:00",
                 "the last 3 years")

    # Day-of-month clamping: Aug 31 minus 6 months can't land on "Feb 31",
    # so it clamps into February's real last day rather than raising.
    aug31 = datetime(2026, 8, 31, 9, 0)
    s, e, lab = resolve_period("6 months", now=aug31)
    check("Aug 31 minus 6 months clamps into February, doesn't error",
          datetime.fromtimestamp(s).strftime("%Y-%m-%d") == "2026-03-01",
          f"got {datetime.fromtimestamp(s)}")

    # Leap day clamping. `start` is computed from the exclusive END (today +
    # 1 day), so the clamp-worthy case is "today" landing the day BEFORE a
    # leap day — that makes END itself Feb 29, one year back from which has
    # to clamp into Feb 28 on the (non-leap) target year rather than raise.
    feb28_leap = datetime(2028, 2, 28, 9, 0)
    s, e, lab = resolve_period("1 year", now=feb28_leap)
    check("a window ending on Feb 29 clamps 1 year back to Feb 28 (2027 isn't leap)",
          datetime.fromtimestamp(s).strftime("%Y-%m-%d") == "2027-02-28",
          f"got {datetime.fromtimestamp(s)}")

    # Out-of-range counts are rejected with unit-specific guidance, same
    # contract as the existing "9999 days" case.
    for bad, unit_word in (("522 weeks", "week"), ("121 months", "month"),
                           ("201 years", "year"), ("0 weeks", "week")):
        try:
            resolve_period(bad, now=NOW)
            check(f"{bad!r} rejected", False, "it was accepted")
        except BadPeriod as e:
            check(f"{bad!r} rejected with guidance", unit_word in str(e),
                  f"msg={e}")


def test_compound_and_ordering() -> None:
    print("\ncompound ranges, in either order")
    a = span("last month and this month")
    b = span("this month and last month")
    check("order doesn't change the window", a[:2] == b[:2])
    check_window("last week to this week", "2026-07-27 00:00", "2026-08-10 00:00")


def test_case_and_whitespace() -> None:
    print("\ncase and spacing are forgiving")
    for variant in ("This Month", "  this   month  ", "THIS MONTH"):
        s, e, _ = resolve_span(variant, now=NOW)
        check(f"{variant!r} matches 'this month'",
              (s, e) == resolve_span("this month", now=NOW)[:2])


def test_bad_input_teaches_the_vocabulary() -> None:
    print("\na bad period tells the model what it CAN say")
    for bad in ("since the dawn of time", "", "2026-13", "9999 days"):
        try:
            resolve_span(bad, now=NOW)
            check(f"{bad!r} rejected", False, "it was accepted")
        except BadPeriod as e:
            msg = str(e)
            teaches = ("this month" in msg or "YYYY-MM" in msg
                       or "range" in msg or "empty" in msg)
            check(f"{bad!r} rejected with guidance", teaches, f"msg={msg[:70]}")


# --------------------------------------------------------------------------
# resolve_when — the schedule_send failure

def when(phrase: str, now: datetime = SEND_NOW):
    return resolve_when(phrase, now=now)


def test_the_schedule_send_reported_case() -> None:
    print("\nthe case that failed: schedule_send 'in 10 minutes'")
    dt, _ = when("in 10 minutes")
    check("'in 10 minutes' at 21:43 -> 21:53, same day (not 09:52 AM)",
          dt == datetime(2026, 8, 19, 21, 53), f"got {dt}")
    check("the resolved time is in the FUTURE relative to now", dt > SEND_NOW)


def test_relative_durations() -> None:
    print("\nrelative durations")
    check("'in 2 hours'", when("in 2 hours")[0] == datetime(2026, 8, 19, 23, 43))
    check("'in 1 hour and 30 minutes'",
          when("in 1 hour and 30 minutes")[0] == datetime(2026, 8, 19, 23, 13))
    check("'10 minutes' (no leading 'in')",
          when("10 minutes")[0] == datetime(2026, 8, 19, 21, 53))
    check("'an hour'", when("an hour")[0] == datetime(2026, 8, 19, 22, 43))
    check("'half an hour'", when("half an hour")[0] == datetime(2026, 8, 19, 22, 13))


def test_clock_times() -> None:
    print("\nbare clock times")
    # 21:43 now: 6pm today has passed, so it must roll to tomorrow.
    check("'6pm' after 6pm has passed -> tomorrow 18:00",
          when("6pm")[0] == datetime(2026, 8, 20, 18, 0))
    check("'18:00' (24h) after it's passed -> tomorrow",
          when("18:00")[0] == datetime(2026, 8, 20, 18, 0))
    check("'6:30am' -> tomorrow 6:30 (today's already gone)",
          when("6:30am")[0] == datetime(2026, 8, 20, 6, 30))
    # Bare '6' with no am/pm: both readings today have passed (6am, 6pm),
    # so the soonest future occurrence is 6am tomorrow.
    check("bare '6' -> nearest future occurrence (6am tomorrow)",
          when("6")[0] == datetime(2026, 8, 20, 6, 0))
    check("'at 9' -> 9am tomorrow (9am/9pm today both passed)",
          when("at 9")[0] == datetime(2026, 8, 20, 9, 0))
    try:
        when("18pm")
        check("'18pm' rejected (24h hour can't take am/pm)", False)
    except BadWhen:
        check("'18pm' rejected (24h hour can't take am/pm)", True)


def test_day_words() -> None:
    print("\nday words and weekdays")
    check("'tonight' -> today 20:00", when("tonight")[0] == datetime(2026, 8, 19, 20, 0))
    check("'tomorrow' (no time) -> tomorrow 9am",
          when("tomorrow")[0] == datetime(2026, 8, 20, 9, 0))
    check("'tomorrow morning' -> tomorrow 9am",
          when("tomorrow morning")[0] == datetime(2026, 8, 20, 9, 0))
    # SEND_NOW is a Wednesday -> the next Monday is 2026-08-24.
    check("'monday' -> next Monday 9am",
          when("monday")[0] == datetime(2026, 8, 24, 9, 0))
    check("'monday morning' -> next Monday 9am",
          when("monday morning")[0] == datetime(2026, 8, 24, 9, 0))
    check("'monday at 9am' -> next Monday 9am",
          when("monday at 9am")[0] == datetime(2026, 8, 24, 9, 0))
    check("'next monday' skips the immediate Monday -> Aug 31",
          when("next monday")[0] == datetime(2026, 8, 31, 9, 0))
    check("'friday evening' -> this Friday 18:00",
          when("friday evening")[0] == datetime(2026, 8, 21, 18, 0))
    # A named weekday whose computed time has already passed rolls a week.
    mon_9am = datetime(2026, 8, 24, 9, 30)  # Monday, just after 9am
    dt, _ = resolve_when("monday morning", now=mon_9am)
    check("'monday morning' said on Monday after 9am -> next Monday",
          dt == datetime(2026, 8, 31, 9, 0), f"got {dt}")


def test_iso_fallback_and_bad_input() -> None:
    print("\nISO fallback and bad input teaches the vocabulary")
    dt, _ = when("2026-08-25T15:00")
    check("bare ISO string still resolves", dt == datetime(2026, 8, 25, 15, 0))
    for bad in ("", "sometime soon", "whenever"):
        try:
            when(bad)
            check(f"{bad!r} rejected", False, "it was accepted")
        except BadWhen as e:
            msg = str(e)
            check(f"{bad!r} rejected with guidance",
                  "in 10 minutes" in msg or "empty" in msg, f"msg={msg[:70]}")


if __name__ == "__main__":
    test_the_reported_case()
    test_months()
    test_month_boundaries_roll_the_year()
    test_weeks_days_years()
    test_new_units()
    test_compound_and_ordering()
    test_case_and_whitespace()
    test_bad_input_teaches_the_vocabulary()
    test_the_schedule_send_reported_case()
    test_relative_durations()
    test_clock_times()
    test_day_words()
    test_iso_fallback_and_bad_input()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
