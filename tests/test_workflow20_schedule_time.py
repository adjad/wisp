"""Scheduled clock phrases reject DST uncertainty; synthetic clocks only."""
import datetime as dt
import asyncio
import os
import time
from zoneinfo import ZoneInfo

import pytest

from service.tools.timeranges import BadWhen, resolve_when


@pytest.fixture(autouse=True)
def local_zone():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    yield
    if previous is None: os.environ.pop("TZ", None)
    else: os.environ["TZ"] = previous
    time.tzset()


@pytest.mark.parametrize("phrase,now", [
    ("2026-11-01T01:30", "2026-10-31T12:00"),
    ("2027-03-14T02:30", "2027-03-13T12:00"),
    ("tomorrow at 1:30am", "2026-10-31T12:00"),
    ("tomorrow at 2:30am", "2027-03-13T12:00"),
    ("1:30am", "2026-11-01T00:30"),
    ("2:30am", "2027-03-14T00:30"),
])
@pytest.mark.parametrize("aware", [False, True])
def test_uncertain_local_wall_times_are_rejected(phrase, now, aware):
    clock = dt.datetime.fromisoformat(now)
    if aware: clock = clock.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    with pytest.raises(BadWhen, match="ambiguous or nonexistent"):
        resolve_when(phrase, now=clock)


@pytest.mark.parametrize("phrase", ["2026-11-01T00:30", "2026-11-01T02:30", "2027-03-14T01:30", "2027-03-14T03:30", "2026-11-01"])
def test_adjacent_valid_times_and_date_only_keep_their_date(phrase):
    result, _ = resolve_when(phrase)
    assert result == dt.datetime.fromisoformat(phrase)


def test_explicit_offsets_keep_absolute_instants():
    early, _ = resolve_when("2026-11-01T01:30-07:00")
    late, _ = resolve_when("2026-11-01T01:30-08:00")
    assert late.timestamp() - early.timestamp() == 3600
    stated, _ = resolve_when("2027-03-14T02:30-08:00")
    assert stated.isoformat() == "2027-03-14T02:30:00-08:00"


@pytest.mark.parametrize("now", ["2026-11-01T00:30", "2027-03-14T01:30"])
@pytest.mark.parametrize("aware", [False, True])
def test_duration_is_elapsed_time_and_survives_iso_serialization(now, aware):
    clock = dt.datetime.fromisoformat(now)
    if aware: clock = clock.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    result, label = resolve_when("in 2 hours", now=clock)
    assert result.timestamp() - clock.timestamp() == 7200
    assert dt.datetime.fromisoformat(result.isoformat()).timestamp() == result.timestamp()
    assert label == "in 2 hours"


def test_explicit_today_is_not_silently_moved_to_tomorrow():
    result, _ = resolve_when("today at 9am", now=dt.datetime(2026,10,10,12))
    assert result == dt.datetime(2026,10,10,9)


@pytest.mark.parametrize('when',['2026-11-01T01:30','2027-03-14T02:30'])
def test_actual_scheduled_send_rejects_uncertain_clock_before_queue_or_contacts(monkeypatch,when):
    from service.tools.action_tools import schedule_send
    def forbidden(*args,**kwargs):
        raise AssertionError('Uncertain schedule must not resolve contacts or touch queue')
    monkeypatch.setattr('service.assistant.outbound_queue.outbound_queue.add',forbidden)
    monkeypatch.setattr('service.tools.action_tools._resolve_recipient',forbidden)
    result=asyncio.run(schedule_send(channel='email',to='Synthetic Contact',
        when=when,body='Synthetic complete message.'))
    assert 'ambiguous or nonexistent' in result
