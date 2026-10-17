"""The Daily Summary composes itself every morning and every evening.

Asked for 2026-10-16: "have it autogenerate in the mornings and evenings from now
on". Before this, the scheduler fired ONE brief a day, at 8am or 8pm (an AM/PM
toggle), and a run missed while the Mac slept was dropped once the 4-hour window
passed. Covered here:

  * TWO RUNS — morning and evening each deliver once a day, under their own keys,
    at configurable hours (defaults 8 AM and 8 PM).
  * CATCH-UP — a launch or wake after a missed run delivers it, but only the
    latest one: waking at 9pm gives the evening summary, not a stale morning one.
  * NO OVERLAP — at most one run is in flight; ticks while it runs start nothing.
  * YIELDING — Wisp has one resident model and the sources a brief reads are the
    ones a live question waits on, so a run never starts during a user turn,
    stands down if one begins mid-run, and that costs it no retry.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-summary-schedule-")
os.environ["WISP_HOME"] = _scratch.name

from service import config, idle                                # noqa: E402
from service.assistant import brief as B                        # noqa: E402
from service.assistant import scheduler                         # noqa: E402
from service.assistant.hub import hub                           # noqa: E402

READY = {"FULL": "Your day.", "TODAY": "Today: nothing.",
         "MESSAGES": "No new messages.", "READY": "1"}
DAY = date(2026, 10, 16)


def _clock_at(hour: int, minute: int = 0):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 16, hour, minute)
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


async def _tick():
    task = scheduler._start_daily_brief()
    if task is not None:
        await task
    return task


def _published_parts(store) -> list[str]:
    return [row["payload"]["part_of_day"] for row in store.pending_events()
            if row["payload"].get("type") == "daily_brief"]


class TestTwoRunsADay:
    @pytest.mark.asyncio
    async def test_morning_and_evening_each_deliver_once(self, _isolated):
        store = _isolated
        with patch.object(B, "_sections", AsyncMock(return_value=READY)):
            for hour in (8, 9, 12):
                with patch.object(scheduler, "datetime", _clock_at(hour)):
                    await _tick()
            assert _published_parts(store) == ["morning"]
            for hour in (20, 21, 23):
                with patch.object(scheduler, "datetime", _clock_at(hour, 30)):
                    await _tick()
        assert _published_parts(store) == ["morning", "evening"]
        keys = {row["dedupe_key"] for row in store.pending_events()}
        assert keys == {"daily_brief:2026-10-16:morning", "daily_brief:2026-10-16:evening"}

    @pytest.mark.asyncio
    async def test_acknowledging_one_run_leaves_the_other_due(self, _isolated):
        store = _isolated
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(8)):
            await _tick()
        row = store.pending_events()[0]
        store.acknowledge_event(row["id"], "daily_brief")
        assert store.completion("daily_brief:morning") == "2026-10-16"
        assert scheduler._pending_slot(datetime(2026, 10, 16, 20)) == (DAY, "evening")
        assert scheduler._pending_slot(datetime(2026, 10, 16, 10)) is None

    @pytest.mark.asyncio
    async def test_configured_hours_move_both_runs(self, _isolated):
        config.set_daily_summary_times(morning=6, evening=17)
        store = _isolated
        with patch.object(B, "_sections", AsyncMock(return_value=READY)):
            with patch.object(scheduler, "datetime", _clock_at(5, 59)):
                await _tick()
            assert _published_parts(store) == []
            with patch.object(scheduler, "datetime", _clock_at(6)):
                await _tick()
            with patch.object(scheduler, "datetime", _clock_at(17)):
                await _tick()
        assert _published_parts(store) == ["morning", "evening"]


class TestCatchUp:
    @pytest.mark.asyncio
    async def test_a_morning_missed_while_asleep_arrives_on_wake(self, _isolated):
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(15)):
            await _tick()
        assert _published_parts(_isolated) == ["morning"]

    @pytest.mark.asyncio
    async def test_only_the_latest_missed_run_is_caught_up(self, _isolated):
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(21)):
            await _tick()
            await _tick()
        assert _published_parts(_isolated) == ["evening"]

    @pytest.mark.asyncio
    async def test_yesterdays_evening_is_not_revived_after_midnight(self, _isolated):
        with patch.object(B, "run_scheduled_brief", AsyncMock(return_value=True)) as run, \
             patch.object(scheduler, "datetime", _clock_at(2)):
            await _tick()
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nothing_starts_before_the_app_connects(self, _isolated, monkeypatch):
        monkeypatch.setattr(hub, "_subs", set())
        with patch.object(B, "run_scheduled_brief", AsyncMock(return_value=False)) as run, \
             patch.object(scheduler, "datetime", _clock_at(8)):
            assert await _tick() is None
        run.assert_not_awaited()
        assert scheduler._brief_attempts == {}


class TestOneRunAtATime:
    @pytest.mark.asyncio
    async def test_a_tick_during_a_run_starts_nothing(self, _isolated):
        gate = asyncio.Event()

        async def slow_sections(part, snapshot=None):
            await gate.wait()
            return READY

        with patch.object(B, "_sections", side_effect=slow_sections) as sections, \
             patch.object(scheduler, "datetime", _clock_at(8)):
            first = scheduler._start_daily_brief()
            await asyncio.sleep(0.05)
            assert first is not None and not first.done()
            assert scheduler._start_daily_brief() is None
            gate.set()
            await first
        assert sections.await_count == 1
        assert _published_parts(_isolated) == ["morning"]


class TestYieldsToTheUser:
    @pytest.mark.asyncio
    async def test_no_run_starts_while_a_turn_is_in_flight(self, _isolated):
        idle.begin_foreground()
        try:
            with patch.object(B, "run_scheduled_brief", AsyncMock(return_value=True)) as run, \
                 patch.object(scheduler, "datetime", _clock_at(8)):
                assert await _tick() is None
            run.assert_not_awaited()
        finally:
            idle.end_foreground()

    @pytest.mark.asyncio
    async def test_a_turn_mid_run_stands_the_run_down_without_costing_a_retry(self, _isolated):
        started, cancelled = asyncio.Event(), []

        async def waiting_on_sources(part, snapshot=None):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(part)
                raise
            return READY

        with patch.object(B, "_sections", side_effect=waiting_on_sources), \
             patch.object(scheduler, "datetime", _clock_at(8)):
            task = scheduler._start_daily_brief()
            await started.wait()
            idle.begin_foreground()
            try:
                await asyncio.wait_for(task, timeout=2)
            finally:
                idle.end_foreground()
        assert cancelled == ["morning"]
        assert _published_parts(_isolated) == []
        assert scheduler._brief_attempts == {}
        # Once the turn is over, the next tick delivers it.
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(8, 1)):
            await _tick()
        assert _published_parts(_isolated) == ["morning"]

    @pytest.mark.asyncio
    async def test_a_turn_that_begins_as_the_brief_finishes_holds_the_publish(self, _isolated):
        async def turn_starts_during(part, snapshot=None):
            idle.begin_foreground()
            return READY

        try:
            with patch.object(B, "_sections", side_effect=turn_starts_during), \
                 patch.object(hub, "publish", new_callable=AsyncMock) as publish:
                assert await B.run_scheduled_brief(
                    "evening", day="2026-10-16", abort=idle.foreground_busy) is None
            publish.assert_not_awaited()
        finally:
            idle.end_foreground()

    @pytest.mark.asyncio
    async def test_the_summary_button_counts_as_the_user_waiting(self, monkeypatch, tmp_path):
        from service import main
        from service.memory.store import SessionStore
        monkeypatch.setattr(main, "store", SessionStore(tmp_path / "sessions.db"))
        seen = []

        async def ensure(timeout_seconds):
            seen.append(idle.foreground_busy())
            return {"syncing": False, "sources": []}

        monkeypatch.setattr("service.assistant.sync_status.ensure_daily_sources", ensure)
        with patch.object(B, "_sections", AsyncMock(return_value=READY)):
            await main.assistant_daily_summary({})
        assert seen == [True] and not idle.foreground_busy()


class TestUpgradeDay:
    """An older version's once-a-day receipt covers only the run it was set to."""

    @pytest.mark.asyncio
    async def test_a_legacy_morning_receipt_still_lets_the_evening_run(self, _isolated):
        store = _isolated
        store.enqueue_event({"type": "daily_brief", "part_of_day": "morning", "text": "x"},
                            dedupe_key="daily_brief:2026-10-16",
                            target={"type": "brief", "date": "2026-10-16"})
        row = store.pending_events()[0]
        store.acknowledge_event(row["id"], "daily_brief")
        assert store.completion("daily_brief") == "2026-10-16"
        assert scheduler._slot_delivered(DAY, "morning")
        assert not scheduler._slot_delivered(DAY, "evening")

    def test_a_legacy_pm_choice_covers_the_evening_run(self, _isolated):
        config.set_daily_summary_hour(20)
        _isolated.enqueue_event({"type": "daily_brief", "part_of_day": "evening", "text": "x"},
                                dedupe_key="daily_brief:2026-10-16",
                                target={"type": "brief", "date": "2026-10-16"})
        assert scheduler._slot_delivered(DAY, "evening")
        assert not scheduler._slot_delivered(DAY, "morning")


class TestConfiguration:
    def test_defaults_are_the_two_times_wisp_already_offered(self):
        assert config.get_daily_summary_times() == {"morning": 8, "evening": 20}

    def test_hours_persist_and_partial_updates_keep_the_other(self):
        assert config.set_daily_summary_times(morning=7) == {"morning": 7, "evening": 20}
        assert config.set_daily_summary_times(evening=18) == {"morning": 7, "evening": 18}
        config._user_overlay.cache_clear()
        config.models_config.cache_clear()
        assert config.get_daily_summary_times() == {"morning": 7, "evening": 18}

    @pytest.mark.parametrize("morning,evening", [
        (12, None), (-1, None), (None, 11), (None, 24), (True, None), (7.5, None), ("7", None)])
    def test_out_of_range_hours_are_refused_and_nothing_is_saved(self, morning, evening):
        with pytest.raises(ValueError):
            config.set_daily_summary_times(morning=morning, evening=evening)
        assert config.get_daily_summary_times() == {"morning": 8, "evening": 20}

    def test_a_corrupt_stored_hour_falls_back_instead_of_disabling(self):
        config._save_overlay({"daily_summary_times": {"morning": 15, "evening": "late"}})
        assert config.get_daily_summary_times() == {"morning": 8, "evening": 20}

    @pytest.mark.asyncio
    async def test_the_schedule_endpoint(self):
        from fastapi import HTTPException
        from service import main
        assert (await main.get_summary_schedule())["times"] == {"morning": 8, "evening": 20}
        result = await main.set_summary_schedule({"times": {"morning": 7, "evening": 19}})
        assert result["times"] == {"morning": 7, "evening": 19}
        for bad in ({"times": {"morning": 13}}, {"times": {"noon": 12}}, {"times": [7, 19]}):
            with pytest.raises(HTTPException) as err:
                await main.set_summary_schedule(bad)
            assert err.value.status_code == 422
        # The legacy AM/PM body still round-trips and leaves the run times alone.
        legacy = await main.set_summary_schedule({"period": "PM"})
        assert legacy["period"] == "PM" and legacy["times"] == {"morning": 7, "evening": 19}
