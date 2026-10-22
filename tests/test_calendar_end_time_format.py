"""get_upcoming(boundary="end") reads as user text: no model-facing scaffolding.

Deterministic calendar reads (workflows.reads.execute_read) show tool text with
no model pass, so "Scheduled Calendar end times (...); these do not verify
actual completion." and "%Y-%m-%d %-I:%M %p %Z%z" stamps reached the user
verbatim. Pure formatter calls on fictional rows; no store, source or model.
"""
import datetime as dt
import os
import time

import pytest


@pytest.fixture
def fmt(monkeypatch):
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    from service.tools import assistant_tools as A
    # 2030-01-02 02:00 PST.
    now = dt.datetime(2030, 1, 2, 10, tzinfo=dt.timezone.utc).timestamp()

    def render(*rows, window="next 60 day(s)"):
        return A._format_calendar_end_times(list(rows), now=now, window_label=window)

    def local(*args):
        return dt.datetime(*args).timestamp()

    yield render, local
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def _event(when, end, title="Physics class", **extra):
    return {"source": "calendar", "title": title, "when_ts": when, "end_ts": end, **extra}


def test_single_event_has_no_scaffolding_or_raw_stamps(fmt):
    render, local = fmt
    text = render(_event(local(2030, 1, 2, 9), local(2030, 1, 2, 10, 15)))
    assert text == ("Next 60 days: 1 event\n\n"
                    "- Physics class (Today · Wed, Jan 2, 9:00 AM) ends at **10:15 AM**\n\n"
                    "These are scheduled end times from your calendar, not a record of "
                    "when anything actually ended.")
    for leaked in ("Scheduled Calendar end times", "do not verify", "Multiple matching",
                   "day(s)", "PST", "-0800", "2030-"):
        assert leaked not in text


def test_multiple_events_are_counted_not_announced(fmt):
    render, local = fmt
    text = render(_event(local(2030, 1, 2, 9), local(2030, 1, 2, 10)),
                  _event(local(2030, 1, 3, 9), local(2030, 1, 3, 10)),
                  window="this week")
    assert text.startswith("This week: 2 events\n\n")
    assert "- Physics class (Today · Wed, Jan 2, 9:00 AM) ends at **10:00 AM**" in text
    assert "- Physics class (Tomorrow · Thu, Jan 3, 9:00 AM) ends at **10:00 AM**" in text


def test_end_on_a_later_day_names_that_day(fmt):
    render, local = fmt
    text = render(_event(local(2030, 1, 2, 22), local(2030, 1, 3, 1, 30), title="Night shift"))
    assert "- Night shift (Today · Wed, Jan 2, 10:00 PM) ends **Tomorrow · Thu, Jan 3, 1:30 AM**" in text


def test_missing_and_inverted_ends_say_so_plainly(fmt):
    render, local = fmt
    start = local(2030, 1, 2, 9)
    for end in (None, start - 60):
        text = render(_event(start, end))
        assert "- Physics class (Today · Wed, Jan 2, 9:00 AM) has no end time on the calendar" in text
        assert "unknown" not in text and "valid end" not in text


def test_all_day_events_use_their_last_day_not_the_exclusive_boundary(fmt):
    render, local = fmt
    one = render(_event(local(2030, 1, 3), local(2030, 1, 4), all_day=True))
    assert "- Physics class (Tomorrow · Thu, Jan 3, all day) is all day, with no set finish time\n" in one
    assert "Jan 4" not in one and "12:00 AM" not in one
    many = render(_event(local(2030, 1, 3), local(2030, 1, 6), title="Field trip", all_day=True))
    assert ("- Field trip (Tomorrow · Thu, Jan 3, all day) is all day, with no set finish time; "
            "it runs through **Sat, Jan 5**") in many


def test_zone_appears_only_across_an_offset_change(fmt):
    render, local = fmt
    # Now is PST; March 10 2030 is after the spring-forward change to PDT.
    text = render(_event(local(2030, 3, 12, 9), local(2030, 3, 12, 10)))
    assert "(Tuesday · Mar 12, 9:00 AM PDT) ends at **10:00 AM PDT**" in text
    assert "PST" not in render(_event(local(2030, 1, 2, 9), local(2030, 1, 2, 10)))


def test_titles_cannot_open_markdown_runs(fmt):
    render, local = fmt
    text = render(_event(local(2030, 1, 2, 9), local(2030, 1, 2, 10), title="**Bold** `code`  review"))
    assert "- Bold code review (Today" in text


def test_header_still_scopes_follow_up_edits_and_classifies_as_a_result(fmt):
    from service.router.router import _AGENDA_COUNTS_RE
    from service.tools.registry import classify_tool_outcome
    render, local = fmt
    text = render(_event(local(2030, 1, 2, 9), local(2030, 1, 2, 10)),
                  _event(local(2030, 1, 3, 9), local(2030, 1, 3, 10)))
    match = _AGENDA_COUNTS_RE.match(text)
    assert match and match.group("events") == "2" and not match.group("reminders")
    assert classify_tool_outcome("get_upcoming", text).status == "succeeded"
