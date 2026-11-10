"""Spoken reminder times on the rules path: noon and midnight as clock times,
and a named day kept when a part-of-day word is said with it."""
from __future__ import annotations

from datetime import datetime

import pytest

from service.reminder_intent import resolve_alert_datetime
from service.tasks.temporal import resolve_named_time

# Tuesday, 10 November 2026, 10:30.
NOW = datetime(2026, 11, 10, 10, 30)


@pytest.mark.parametrize("text, expected", [
    ("remind me at noon to call", datetime(2026, 11, 10, 12, 0)),
    ("remind me at midnight to stretch", datetime(2026, 11, 11, 0, 0)),
    ("remind me tomorrow noon to call", datetime(2026, 11, 11, 12, 0)),
    ("remind me tomorrow at noon to call", datetime(2026, 11, 11, 12, 0)),
    ("remind me friday noon to call", datetime(2026, 11, 13, 12, 0)),
    ("remind me tomorrow midnight to stretch", datetime(2026, 11, 11, 0, 0)),
    ("remind me tomorrow at midnight to stretch", datetime(2026, 11, 11, 0, 0)),
    ("remind me tonight at midnight to stretch", datetime(2026, 11, 11, 0, 0)),
    # Digit clocks are untouched.
    ("remind me tomorrow at 9am to call", datetime(2026, 11, 11, 9, 0)),
    ("remind me at 6pm to call", datetime(2026, 11, 10, 18, 0)),
])
def test_noon_and_midnight_resolve_on_both_reminder_paths(text, expected):
    assert resolve_alert_datetime(text, now=NOW) == expected
    assert resolve_named_time(text, now=NOW) == (expected, "")


def test_a_named_date_keeps_its_day_at_noon():
    # The date was already kept for "sept 28 at 7pm"; "at noon" asked instead.
    assert resolve_named_time("remind me nov 28 at noon to call", now=NOW) == (
        datetime(2026, 11, 28, 12, 0), "")
    assert resolve_named_time("remind me nov 28 at 7pm to call", now=NOW) == (
        datetime(2026, 11, 28, 19, 0), "")


@pytest.mark.parametrize("text, expected", [
    # A part of day said with a day is that day's.
    ("remind me friday evening to call mom", datetime(2026, 11, 13, 18, 0)),
    ("remind me monday morning", datetime(2026, 11, 16, 9, 0)),
    ("remind me tomorrow night", datetime(2026, 11, 11, 20, 0)),
    ("remind me tomorrow in the evening to call", datetime(2026, 11, 11, 18, 0)),
    ("remind me friday evening at 7 to call", datetime(2026, 11, 13, 19, 0)),
    ("remind me tomorrow morning at 7", datetime(2026, 11, 11, 7, 0)),
    ("remind me tonight at 9", datetime(2026, 11, 10, 21, 0)),
    ("remind me to call mom friday evening", datetime(2026, 11, 13, 18, 0)),
    # On its own it is still the next one, today when it has not passed.
    ("remind me this evening", datetime(2026, 11, 10, 18, 0)),
    ("remind me in the evening", datetime(2026, 11, 10, 18, 0)),
    ("remind me in the morning", datetime(2026, 11, 11, 9, 0)),
    ("remind me next morning", datetime(2026, 11, 11, 9, 0)),
    ("remind me this afternoon to call", datetime(2026, 11, 10, 15, 0)),
    # A day word elsewhere in the sentence does not move "this evening".
    ("remind me this evening about friday's meeting", datetime(2026, 11, 10, 18, 0)),
    ("remind me this evening about the friday night party", datetime(2026, 11, 10, 18, 0)),
    ("remind me tonight", datetime(2026, 11, 10, 20, 0)),
    ("remind me tomorrow", datetime(2026, 11, 11, 9, 0)),
])
def test_a_part_of_day_said_with_a_day_keeps_that_day(text, expected):
    assert resolve_alert_datetime(text, now=NOW) == expected
