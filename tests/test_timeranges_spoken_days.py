"""Weekdays, weekends and dates without a year, as people say them.

Found by a 1,040-question Ask run (2026-10-29): get_upcoming, find_free_time and
daily_brief rejected "Friday", "this weekend", "next Monday", "nov 3" and
"tonight". The model then gave up, asked the user, or answered "Sunday is clear"
from a call that only covered Saturday. Wisp resolves the words; a schedule
reads them forward ("what's on Friday" is the coming one), history backward
("emails from Friday" is the last one), and today counts as either.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from service.tools.timeranges import BadPeriod, resolve_span

THU = datetime(2026, 10, 29, 1, 30)   # the run's own date: Thursday
SAT = datetime(2026, 10, 31, 10, 0)


def day(period, *, now=THU, forward=False):
    s, e, label = resolve_span(period, now=now, forward=forward)
    return (f"{datetime.fromtimestamp(s):%a %m-%d}", f"{datetime.fromtimestamp(e):%a %m-%d}", label)


@pytest.mark.parametrize("period", ["friday", "Friday", "on Friday", "fri", "this friday", "Friday?"])
def test_a_schedule_reads_a_weekday_forward(period):
    assert day(period, forward=True) == ("Fri 10-30", "Sat 10-31", "Friday, October 30")


@pytest.mark.parametrize("period", ["friday", "on friday", "last friday"])
def test_history_reads_a_weekday_backward(period):
    assert day(period)[:2] == ("Fri 10-23", "Sat 10-24")


def test_today_is_either_way_and_last_or_next_skip_it():
    assert day("thursday", forward=True)[0] == day("thursday")[0] == "Thu 10-29"
    assert day("next thursday", forward=True)[0] == "Thu 11-05"
    assert day("last thursday")[0] == "Thu 10-22"
    assert day("next monday", forward=True)[0] == "Mon 11-02"


def test_weekends_run_saturday_to_monday():
    assert day("this weekend", forward=True)[:2] == ("Sat 10-31", "Mon 11-02")
    assert day("the weekend", forward=True)[:2] == ("Sat 10-31", "Mon 11-02")
    assert day("last weekend")[:2] == ("Sat 10-24", "Mon 10-26")
    assert day("over the weekend")[:2] == ("Sat 10-24", "Mon 10-26")
    # On a Saturday, "this weekend" is the one under way, both ways.
    assert day("this weekend", now=SAT, forward=True)[:2] == ("Sat 10-31", "Mon 11-02")
    assert day("weekend", now=SAT)[:2] == ("Sat 10-31", "Mon 11-02")
    assert day("next weekend", now=SAT, forward=True)[:2] == ("Sat 11-07", "Mon 11-09")


def test_parts_of_today_are_today():
    for p in ("tonight", "this evening", "this afternoon", "later today", "rest of the day"):
        assert day(p, forward=True) == ("Thu 10-29", "Fri 10-30", "today")


def test_dates_without_a_year():
    assert day("nov 3", forward=True)[:2] == ("Tue 11-03", "Wed 11-04")
    assert day("November 1st", forward=True)[0] == "Sun 11-01"
    assert day("3rd of november", forward=True)[0] == "Tue 11-03"
    assert day("the 3rd", forward=True)[0] == "Tue 11-03"     # said on the 29th: next month
    assert day("the 3rd")[0] == "Sat 10-03"                   # history: this month's
    assert day("oct 20")[0] == "Tue 10-20"
    # History a few days ahead stays this year; far ahead is last year's, and says so.
    assert day("nov 3")[0] == "Tue 11-03"
    assert day("december 25")[2] == "Thursday, December 25, 2025"
    assert day("oct 20", forward=True)[2] == "Wednesday, October 20, 2027"


def test_since_a_weekday_runs_to_the_end_of_today():
    assert day("since monday")[:2] == ("Mon 10-26", "Fri 10-30")


def test_wrappers_and_old_words_still_work():
    assert day("in the past 3 days")[:2] == ("Tue 10-27", "Fri 10-30")
    assert day("tomorrow", forward=True)[:2] == ("Fri 10-30", "Sat 10-31")
    assert day("this week and next week")[:2] == ("Mon 10-26", "Mon 11-09")
    assert day("2026-10-31")[0] == "Sat 10-31"


@pytest.mark.parametrize("bad", ["sometime", "feb 30", "the 31st of november", "blursday"])
def test_nonsense_still_teaches_the_vocabulary(bad):
    with pytest.raises(BadPeriod) as e:
        resolve_span(bad, now=THU)
    assert "weekday" in str(e.value) or "no " in str(e.value)


def test_get_upcoming_reads_periods_forward():
    import inspect

    from service.tools import assistant_tools
    assert "resolve_span(period, forward=True)" in inspect.getsource(assistant_tools.get_upcoming)


def test_free_time_and_the_brief_take_weekdays_forward():
    import datetime as dt
    import inspect

    from service.tools import everyday, schedule_extras
    now = dt.datetime(2026, 10, 29, 1, 30)
    assert schedule_extras._availability_days(now, 3, "friday") == [dt.date(2026, 10, 30)]
    assert schedule_extras._availability_days(now, 3, "this weekend") == [
        dt.date(2026, 10, 31), dt.date(2026, 11, 1)]
    assert schedule_extras._availability_days(now, 3, "tomorrow") == [dt.date(2026, 10, 30)]
    with pytest.raises(ValueError, match="weekday"):
        schedule_extras._availability_days(now, 3, "blursday")
    assert "resolve_span(period, now=now, forward=True)" in inspect.getsource(everyday.daily_brief)


# dev8 audit: year and month edges, a weekend asked on Sunday, DST days.
DEC30 = datetime(2026, 12, 30, 9, 0)


def test_dates_roll_over_new_year():
    assert day("jan 2", now=DEC30, forward=True)[:2] == ("Sat 01-02", "Sun 01-03")
    assert resolve_span("jan 2", now=DEC30, forward=True)[2].endswith("2027")
    assert day("next friday", now=DEC30, forward=True)[0] == "Fri 01-01"
    assert day("this weekend", now=DEC30, forward=True)[:2] == ("Sat 01-02", "Mon 01-04")
    s, _, label = resolve_span("dec 28", now=datetime(2027, 1, 3, 9))
    assert f"{datetime.fromtimestamp(s):%Y-%m-%d}" == "2026-12-28" and label.endswith("2026")


def test_february_29():
    assert day("feb 29", now=datetime(2028, 2, 20, 9), forward=True)[0] == "Tue 02-29"
    # Ahead from a non-leap February, the next Feb 29 is next year's.
    assert resolve_span("feb 29", now=datetime(2027, 2, 20, 9), forward=True)[2].endswith("2028")
    with pytest.raises(BadPeriod, match="no February 29 in 2026"):
        resolve_span("feb 29", now=datetime(2027, 2, 20, 9))
    # After this year's Feb 29 the next one is years away: named, never Feb 28.
    with pytest.raises(BadPeriod, match="2029"):
        resolve_span("feb 29", now=datetime(2028, 3, 5, 9), forward=True)
    # "the 29th" in a short February is the next month that has one.
    assert day("the 29th", now=datetime(2027, 2, 20, 9), forward=True)[0] == "Mon 03-29"
    assert day("the 31st", now=datetime(2026, 4, 2, 9), forward=True)[0] == "Sun 05-31"


def test_a_weekend_asked_on_sunday():
    sun = datetime(2026, 11, 1, 10, 0)
    assert day("this weekend", now=sun, forward=True)[:2] == ("Sat 10-31", "Mon 11-02")
    assert day("next weekend", now=sun, forward=True)[:2] == ("Sat 11-07", "Mon 11-09")
    assert day("last weekend", now=sun)[:2] == ("Sat 10-24", "Mon 10-26")


def test_days_across_a_dst_change_end_at_local_midnight():
    """Nov 1 2026 and Mar 14 2027 are 25 and 23 hours long in US time zones; the
    window still runs midnight to midnight local, whatever the zone."""
    for period, now in (("sunday", datetime(2026, 10, 31, 10)), ("mar 14", datetime(2027, 3, 10, 10))):
        s, e, _ = resolve_span(period, now=now, forward=True)
        start, end = datetime.fromtimestamp(s), datetime.fromtimestamp(e)
        assert (start.hour, start.minute, end.hour, end.minute) == (0, 0, 0, 0)
        assert (end.date() - start.date()).days == 1
