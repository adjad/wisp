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
