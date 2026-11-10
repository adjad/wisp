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


# Tuesday, 10 November 2026, 14:00, for the typed-task resolver.
AFTERNOON = datetime(2026, 11, 10, 14, 0)


@pytest.mark.parametrize("text, expected", [
    ("tomorrow 7pm", datetime(2026, 11, 11, 19, 0)),
    ("tomorrow at 7", datetime(2026, 11, 11, 19, 0)),
    ("tomorrow at 7:30", datetime(2026, 11, 11, 19, 30)),
    ("friday at 7", datetime(2026, 11, 13, 19, 0)),
    ("tomorrow morning at 8", datetime(2026, 11, 11, 8, 0)),
    ("at 7pm on friday", datetime(2026, 11, 13, 19, 0)),
    ("at 7 tomorrow", datetime(2026, 11, 11, 19, 0)),
    ("nov 15 at 5", datetime(2026, 11, 15, 17, 0)),
    ("nov 15 at 17:30", datetime(2026, 11, 15, 17, 30)),
    ("friday night at 12", datetime(2026, 11, 14, 0, 0)),
    # Unchanged forms.
    ("friday at 7pm", datetime(2026, 11, 13, 19, 0)),
    ("7pm friday", datetime(2026, 11, 13, 19, 0)),
    ("nov 15 at 5pm", datetime(2026, 11, 15, 17, 0)),
    ("tomorrow at noon", datetime(2026, 11, 11, 12, 0)),
])
def test_a_clock_said_with_a_day_is_that_clock(text, expected):
    assert resolve_named_time(text, now=AFTERNOON) == (expected, "")


@pytest.mark.parametrize("text, expected", [
    ("tomorrow", (datetime(2026, 11, 11, 9, 0), "morning")),
    ("nov 15", (datetime(2026, 11, 15, 9, 0), "morning")),
    ("tonight", (datetime(2026, 11, 10, 20, 0), "tonight")),
    ("tomorrow evening", (datetime(2026, 11, 11, 18, 0), "evening")),
    # Digits that are not a clock leave the day default alone.
    ("tomorrow 3 pages", (datetime(2026, 11, 11, 9, 0), "morning")),
])
def test_a_day_without_a_clock_keeps_its_default(text, expected):
    assert resolve_named_time(text, now=AFTERNOON) == expected


@pytest.mark.parametrize("text", [
    "the day after tomorrow",
    "on the 15th at 6pm",
    "on the 15th",
    # A clock the day phrase did not take is not replaced by 9 AM.
    "tomorrow to at 7pm",
    "tomorrow at 7 a.m.",
    "this evening at 7",
])
def test_a_time_it_cannot_place_is_not_guessed(text):
    assert resolve_named_time(text, now=AFTERNOON) == (None, "")


@pytest.mark.parametrize("text, expected", [
    ("remind me tomorrow 7pm to call mom", "2026-11-11T19:00"),
    ("remind me to call mom tomorrow at 7:30", "2026-11-11T19:30"),
    ("remind me to call mom at 7pm on friday", "2026-11-13T19:00"),
    ("remind me to pay rent on nov 15 at 5", "2026-11-15T17:00"),
    ("remind me to call mom tomorrow morning at 8", "2026-11-11T08:00"),
    ("remind me to call mom tomorrow", "2026-11-11T09:00"),
])
def test_typed_reminder_uses_the_stated_clock(text, expected):
    from service.tasks.compiler import compile_reminder_create
    plan = compile_reminder_create(text, now=AFTERNOON)
    assert plan is not None and plan.status == "ready"
    assert plan.temporal.absolute_iso == expected


@pytest.mark.parametrize("text", [
    "remind me to call mom on the 15th at 6pm",
    "remind me the day after tomorrow to renew the passport",
    "remind me tomorrow to call mom at 7pm",
])
def test_typed_reminder_does_not_file_an_unplaced_time(text):
    from service.tasks.compiler import compile_reminder_create
    plan = compile_reminder_create(text, now=AFTERNOON)
    assert plan is None or (plan.status != "ready" and not plan.temporal.absolute_iso)


@pytest.mark.parametrize("text", [
    # A clock only inside the quoted title is title text, not the alert.
    'remind me tomorrow to "review at 6pm"',
    # Digits after "at" that are not a clock.
    "remind me tomorrow to look at 3 houses",
])
def test_a_clock_that_is_title_text_keeps_the_day_default(text):
    from service.tasks.compiler import compile_reminder_create
    plan = compile_reminder_create(text, now=AFTERNOON)
    assert plan is not None and plan.status == "ready"
    assert plan.temporal.absolute_iso == "2026-11-11T09:00"
