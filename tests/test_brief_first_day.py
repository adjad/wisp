"""What a new user is told about when Wisp acts, and the first Brief's progress.

  * rhythm_line() says the summary times, the check-in hour and the routine bar
    from the settings that decide them, never a guess;
  * the composed Brief carries it, as its "How Wisp works" section, on the
    first launch day only;
  * first_brief_building() holds while the first launch day's first summary is
    on its way, never while the run is held for first-launch permissions, and
    stops once it was delivered or its retries ran out.

Isolated: a scratch WISP_HOME and store; nothing is composed from real sources.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-brief-first-day-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from service import config, idle                                # noqa: E402
from service.assistant import brief as B                        # noqa: E402
from service.assistant import checkin                           # noqa: E402
from service.assistant import scheduler                         # noqa: E402
from service.assistant.hub import hub                           # noqa: E402
from service.assistant.store import FIRST_LAUNCH_KEY            # noqa: E402
from service.behavior import miners                             # noqa: E402

READY = {"FULL": "Your day.", "TODAY": "Today: nothing.",
         "MESSAGES": "No new messages.", "READY": "1"}
INSTALLED = datetime(2026, 10, 16, 5, 30).timestamp()


def _clock_at(hour: int, minute: int = 0, day: int = 16):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, day, hour, minute)
    return Clock


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "cfg" / "config.yaml")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    monkeypatch.setattr(scheduler, "_brief_state_path", lambda: tmp_path / "brief_state.json")
    monkeypatch.setattr(scheduler, "_last_brief", None)
    monkeypatch.setattr(scheduler, "_last_brief_loaded", False)
    monkeypatch.setattr(scheduler, "_brief_attempts", {})
    monkeypatch.setattr(scheduler, "_brief_task", None)
    monkeypatch.setattr(scheduler, "_YIELD_POLL_S", 0.01)
    monkeypatch.setattr(scheduler, "_know_you_completed_at", lambda: None)
    monkeypatch.setattr(B, "_uses_24_hour", lambda: False)
    from service.assistant.store import AssistantStore
    store = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(scheduler, "assistant_store", store)
    monkeypatch.setattr(B, "assistant_store", store)
    monkeypatch.setattr(hub, "_store", store)
    monkeypatch.setattr(hub, "_subs", {asyncio.Queue()})
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    yield store
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


def _first_day(store) -> None:
    store.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
    store.record_completion(scheduler.ONBOARDING_ANSWERED_KEY, repr(time.time()))


class TestRhythm:
    def test_says_the_configured_times(self, monkeypatch):
        monkeypatch.setattr("service.behavior_switch.enabled", lambda: True)
        assert config.get_daily_summary_times() == {"morning": 8, "evening": 20}
        assert checkin.CHECKIN_HOUR == 17 and miners.HABIT_MIN_WEEKS == 3
        assert B.rhythm_line() == ("I send a summary at 8 AM and 8 PM, ask how your day went after "
                                   "5 PM, and learn a routine once I've seen it in three different weeks.")
        config.set_daily_summary_times(morning=7, evening=18)
        assert B.rhythm_line().startswith("I send a summary at 7 AM and 6 PM,")

    def test_no_routine_promise_while_learning_is_off(self, monkeypatch):
        monkeypatch.setattr("service.behavior_switch.enabled", lambda: False)
        assert B.rhythm_line() == "I send a summary at 8 AM and 8 PM and ask how your day went after 5 PM."

    def test_twenty_four_hour_clock(self, monkeypatch):
        monkeypatch.setattr(B, "_uses_24_hour", lambda: True)
        monkeypatch.setattr("service.behavior_switch.enabled", lambda: False)
        assert B.rhythm_line().startswith("I send a summary at 08:00 and 20:00 and ask how your day went after 17:00")


class TestFirstDaySection:
    def test_only_on_the_first_launch_day(self, _isolated, monkeypatch):
        monkeypatch.setattr(B, "rhythm_line", lambda: "I send a summary at 8 AM and 8 PM.")
        assert B._first_day_section(INSTALLED + 3600) == ""   # no marker: an upgraded install
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
        assert B._first_day_section(INSTALLED + 3600) == "**🌱 How Wisp works**\n- I send a summary at 8 AM and 8 PM."
        assert B._first_day_section(INSTALLED + 86400) == ""

    def test_sits_right_after_the_greeting(self, _isolated, monkeypatch):
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
        for name in ("_keeping_in_mind", "_schedule_section", "_email_section"):
            monkeypatch.setattr(B, name, lambda now, name=name: f"**{name}**")
        monkeypatch.setattr(B, "_greeting_line", lambda now: "Good morning.")
        monkeypatch.setattr(B, "rhythm_line", lambda: "I send a summary.")
        text = B._render_brief(INSTALLED + 3600, "- Nothing new.")
        assert text.split("\n\n")[:3] == ["Good morning.", "**🌱 How Wisp works**\n- I send a summary.",
                                          "**_keeping_in_mind**"]

    def test_never_breaks_the_brief(self, _isolated, monkeypatch):
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
        monkeypatch.setattr(B, "rhythm_line", lambda: (_ for _ in ()).throw(RuntimeError("config")))
        assert B._first_day_section(INSTALLED + 3600) == ""


class TestFirstBriefBuilding:
    def test_due_on_the_first_day_until_delivered(self, _isolated):
        assert not scheduler.first_brief_building(_clock_at(6, 30).now())   # no first launch marker
        _first_day(_isolated)
        assert scheduler.first_brief_building(_clock_at(6, 30).now())
        assert not scheduler.first_brief_building(_clock_at(4, 0).now())     # not due in the small hours
        assert not scheduler.first_brief_building(_clock_at(6, 30, day=17).now())

    @pytest.mark.asyncio
    async def test_stops_once_delivered(self, _isolated):
        _first_day(_isolated)
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(6, 30)):
            task = scheduler._start_daily_brief()
            assert task is not None and scheduler.first_brief_building(_clock_at(6, 30).now())
            await task
        assert not scheduler.first_brief_building(_clock_at(6, 31).now())
        # The evening run is due later that day, but the first Brief already came.
        assert not scheduler.first_brief_building(_clock_at(20, 0).now())

    def test_stops_when_its_retries_ran_out(self, _isolated):
        _first_day(_isolated)
        now = _clock_at(6, 30).now()
        scheduler._brief_attempts[(now.date(), "morning")] = scheduler._MAX_BRIEF_ATTEMPTS
        assert not scheduler.first_brief_building(now)

    def test_not_while_held_for_first_launch_permissions(self, _isolated, monkeypatch):
        # Installed ten minutes ago, Calendar never answered, onboarding not
        # done: the run waits (FIRST_LAUNCH_GRACE_S), so nothing is building.
        now = _clock_at(6, 30).now()
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(now.timestamp() - 600))
        monkeypatch.setattr(scheduler, "_never_answered_sources", lambda: ["calendar"])
        assert scheduler._awaiting_first_permission(now.timestamp())
        assert not scheduler.first_brief_building(now)
        # Once onboarding is answered the hold lifts and the Brief is on its way.
        _isolated.record_completion(scheduler.ONBOARDING_ANSWERED_KEY, repr(now.timestamp()))
        assert scheduler.first_brief_building(now)


def test_the_schedule_endpoint_says_whether_the_first_brief_is_building(monkeypatch):
    from service import main
    monkeypatch.setattr(main.assistant_scheduler, "first_brief_building", lambda: True)
    assert main._summary_schedule()["first_brief_building"] is True
    def broken():
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(main.assistant_scheduler, "first_brief_building", broken)
    schedule = main._summary_schedule()
    assert schedule["first_brief_building"] is False and "times" in schedule
