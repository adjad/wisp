"""Debug export 2026-10-19 (wisp-debug-2026-10-19_00-19-54): two reported faults.

1. "what is on my calender this week" was answered by a deterministic read with
   no model pass, so get_upcoming's model-facing header reached the user as-is:
   "Upcoming — this week (9 item(s))\nCalendar events: 9; Wisp/Apple reminders:
   0. [Calendar event] and [Reminder] are shown in separate sections."
2. "what should i get ready for tommorow" matched no rule, fell to the
   retrieved menu without get_upcoming, and the model's get_upcoming call was
   refused, so Wisp answered "I can't retrieve tomorrow's calendar details".
"""
import asyncio
from datetime import datetime

import pytest

import service.tools  # noqa: F401  register the roster
import service.router.router as R
from service.tools import assistant_tools as A

NOW = datetime(2026, 10, 19, 0, 19).timestamp()


def _row(title, day, hour, minute=0, location="", source="calendar"):
    return {"id": title, "source": source, "title": title, "location": location,
            "when_ts": datetime(2026, 10, day, hour, minute).timestamp()}


def _week():
    return [
        _row("STAT 5-02 Office Hours", 19, 9, 30, "Zoom Online Meeting"),
        _row("Dublin Community Office Hours", 19, 18, 0, "Paris Baguette"),
        _row("Camp retreat", 20, 6, 30),
        _row("Pay rent", 20, 9, 0, source="reminders"),
        _row("CruzHacks Hack Day", 24, 11, 45),
    ]


def test_agenda_has_no_model_scaffolding_and_reads_by_day():
    text = A._format_forward_agenda(_week(), now=NOW, window_label="this week")
    for scaffold in ("item(s)", "[Calendar event]", "[Reminder]", "separate sections",
                     "Calendar events:", "Wisp/Apple reminders", "PDT-0700", "PST-0800"):
        assert scaffold not in text, scaffold
    assert text.startswith("This week: 4 events, 1 reminder\n\n")
    assert "**Today · Mon, Oct 19**\n- **9:30 AM** — STAT 5-02 Office Hours · Zoom Online Meeting" in text
    # Reminders stay apart from that day's calendar events.
    assert ("**Tomorrow · Tue, Oct 20**\n- **6:30 AM** — Camp retreat\n*Reminders*\n"
            "- **9:00 AM** — Pay rent") in text
    assert "**Saturday · Oct 24**" in text


def test_agenda_markdown_is_safe_from_titles():
    text = A._format_forward_agenda([_row("Send **report** `now`", 19, 10)], now=NOW,
                                    window_label="today")
    assert "Send report now" in text


def test_agenda_says_no_reminders_only_when_reminders_were_read():
    rows = [_row("Camp retreat", 20, 6, 30)]
    assert A._format_forward_agenda(rows, now=NOW, window_label="tomorrow").startswith(
        "Tomorrow: 1 event, no reminders\n")
    assert A._format_forward_agenda(rows, now=NOW, window_label="tomorrow",
                                    reminders_read=False).startswith("Tomorrow: 1 event\n")
    assert A._format_forward_agenda(rows, now=NOW, window_label="next 7 day(s)").startswith(
        "Next 7 days: ")


@pytest.mark.parametrize("header,kind", [
    ("This week: 9 events, no reminders", "calendar event edit"),
    ("Today: no events, 3 reminders", "reminder edit"),
    ("Tomorrow: 2 events, 1 reminder", "calendar/reminder edit"),
    ("Next 7 days: 1 event", "calendar event edit"),
])
def test_edit_rule_reads_the_new_agenda_counts(header, kind):
    decision = R._edit_reference_subset("move the first one to 4pm",
                                        header + "\n\n**Today · Mon, Oct 19**\n- **9:30 AM** — X",
                                        "get_upcoming")
    assert decision is not None and decision.reason.startswith(kind), decision.reason


def _full_route(text, **context):
    from tests.stub_embedder import install
    install()
    return asyncio.run(R.route(text, **context))


@pytest.mark.parametrize("prompt", [
    "what should i get ready for tommorow",   # the exported prompt, typo included
    "what should i get ready for tomorrow",
    "What should I prepare for tomorrow?",
    "what do i need to get ready for tmrw",
])
def test_getting_ready_for_tomorrow_reads_tomorrows_calendar(prompt):
    previous = A._format_forward_agenda(_week(), now=NOW, window_label="this week")
    decision = _full_route(prompt, last_user="what is on my calender this week",
                           last_assistant=previous, last_tools="get_upcoming")
    assert ("get_upcoming", {"period": "tomorrow"}) in decision.direct_calls, decision.reason
    assert "get_upcoming" not in decision.forbidden_tools


@pytest.mark.parametrize("prompt", [
    "what should i get ready for this week", "what should i prep for friday",
    "what should i prepare for tonight",
])
def test_getting_ready_for_a_named_day_offers_the_schedule(prompt):
    decision = R.rule_route(prompt)
    assert decision is not None and "get_upcoming" in decision.tool_subset, prompt


@pytest.mark.parametrize("prompt", [
    "what should i get ready for my interview", "what should i prepare for a trip to japan",
])
def test_preparing_for_a_thing_is_not_a_schedule_lookup(prompt):
    assert not R._TODO_RE.search(prompt)
    assert not R._TOMORROW_TODO_RE.fullmatch(prompt)
