"""The first Daily Summary on a new install is not composed from sources the user
is still granting, and is rebuilt once when they arrive or Getting to know you ends.

  * GRACE — for 15 minutes after first launch, a due run waits (without using a
    retry) while Calendar, Reminders or Messages is unavailable only because its
    permission was never answered. A denial is an answer: that does not wait.
  * REBUILD — on the first launch day, each delivered run is rebuilt at most
    once: when a source it could not read becomes ready, or Getting to know you
    completes after it, whichever comes first.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-brief-day-one-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from service import config, idle                                # noqa: E402
from service.assistant import brief as B                        # noqa: E402
from service.assistant import scheduler                         # noqa: E402
from service.assistant import sync_status                       # noqa: E402
from service.assistant.hub import hub                           # noqa: E402
from service.assistant.store import FIRST_LAUNCH_KEY            # noqa: E402

READY = {"FULL": "Your day.", "TODAY": "Today: nothing.",
         "MESSAGES": "No new messages.", "READY": "1"}
DAY = date(2026, 10, 16)
INSTALLED = datetime(2026, 10, 16, 8, 30).timestamp()


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
    monkeypatch.setattr(scheduler, "_sync_status", {
        "calendar": {"available": False, "reason": "waiting for the app to sync", "count": 0},
        "reminders": {"available": False, "reason": "waiting for the app to sync", "count": 0},
    })
    monkeypatch.setattr(scheduler, "_know_you_completed_at", lambda: None)
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


def _report(source: str, **diagnostics) -> None:
    scheduler._sync_status[source] = {"available": False, "syncing": bool(diagnostics.get("syncing")),
                                      "reason": diagnostics.get("reason", ""), "count": 0,
                                      "last_sync": time.time(), "diagnostics": diagnostics}


def _snapshot(**states: str) -> dict:
    sources = [{"id": source, "label": source.title(), "state": states.get(source, "ready")}
               for source in ("calendar", "reminders", "email", "messages")]
    return {"sources": sources, "syncing": any(s["state"] == "syncing" for s in sources)}


def _brief_keys(store) -> list[str]:
    with store._lock:
        return [r[0] for r in store._db.execute(
            "SELECT dedupe_key FROM assistant_events WHERE kind='daily_brief' ORDER BY created_at,id")]


async def _run(task):
    if task is not None:
        await task
    return task


# --- grace ----------------------------------------------------------------------

class TestFirstLaunchGrace:
    @pytest.mark.asyncio
    async def test_never_answered_calendar_holds_the_first_run(self, _isolated):
        _report("calendar", authorized=False, syncing=False, status=0)
        with patch.object(B, "run_scheduled_brief", AsyncMock(return_value=True)) as run, \
             patch.object(scheduler, "datetime", _clock_at(9)):
            assert await _run(scheduler._start_daily_brief()) is None
        run.assert_not_awaited()
        assert scheduler._brief_attempts == {}

    @pytest.mark.asyncio
    async def test_grace_ends_after_fifteen_minutes(self, _isolated):
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(time.time() - scheduler.FIRST_LAUNCH_GRACE_S - 1))
        _report("calendar", authorized=False, syncing=False, status=0)
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(9)):
            assert await _run(scheduler._start_daily_brief()) is not None
        assert _brief_keys(_isolated) == ["daily_brief:2026-10-16:morning"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source,diagnostics", [
        ("calendar", {"authorized": False, "syncing": False, "status": 2}),
        ("reminders", {"authorized": False, "syncing": False,
                       "reason": "Reminders access is turned off for Wisp. Turn it on in System Settings."}),
    ])
    async def test_a_denial_is_an_answer(self, _isolated, source, diagnostics):
        _report(source, **diagnostics)
        assert scheduler._never_answered_sources() == []
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(9)):
            assert await _run(scheduler._start_daily_brief()) is not None

    def test_unanswered_reminders_prompt_counts(self, _isolated):
        _report("reminders", authorized=False, syncing=False,
                reason="The Reminders permission prompt is still waiting for an answer.")
        assert scheduler._never_answered_sources() == ["reminders"]
        assert scheduler._awaiting_first_permission(time.time())

    def test_messages_without_full_disk_access_counts(self, _isolated):
        with patch("service.tools.imessage_tools.messages_sync_state", lambda: "unavailable"), \
             patch("service.tools.imessage_tools.messages_unavailable_kind", lambda: "full_disk_access"):
            assert scheduler._never_answered_sources() == ["messages"]

    def test_upgraded_install_never_waits(self, _isolated):
        _isolated.record_completion(FIRST_LAUNCH_KEY, "0")
        _report("calendar", authorized=False, syncing=False, status=0)
        assert not scheduler._awaiting_first_permission(time.time())

    def test_ready_sources_never_wait(self, _isolated):
        assert scheduler._never_answered_sources() == []
        assert not scheduler._awaiting_first_permission(time.time())


# --- rebuild ----------------------------------------------------------------------

async def _deliver_day_one(store, gaps: dict) -> None:
    store.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
    with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
         patch.object(scheduler, "datetime", _clock_at(9)), \
         patch.object(sync_status, "summary_snapshot", lambda: _snapshot(**gaps)):
        assert await _run(scheduler._start_daily_brief()) is not None


class TestDayOneRebuild:
    @pytest.mark.asyncio
    async def test_rebuilt_once_when_a_missing_source_arrives(self, _isolated):
        await _deliver_day_one(_isolated, {"calendar": "unavailable"})
        record = json.loads(_isolated.completion(scheduler._DAY_ONE_KEY))
        assert record["gaps"] == ["calendar"] and record["part"] == "morning"
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(10)):
            with patch.object(sync_status, "summary_snapshot", lambda: _snapshot(calendar="unavailable")):
                assert scheduler._start_day_one_recompose() is None
            with patch.object(sync_status, "summary_snapshot", lambda: _snapshot(calendar="syncing")):
                assert scheduler._start_day_one_recompose() is None
            with patch.object(sync_status, "summary_snapshot", lambda: _snapshot()):
                assert await _run(scheduler._start_day_one_recompose()) is not None
                assert scheduler._start_day_one_recompose() is None    # once
        assert _brief_keys(_isolated) == ["daily_brief:2026-10-16:morning",
                                          "daily_brief:2026-10-16:morning:rebuilt"]
        rebuilt = _isolated.event_by_key("daily_brief:2026-10-16:morning:rebuilt")
        assert rebuilt["payload"]["recomposed"] is True
        assert "recomposed" not in _isolated.event_by_key("daily_brief:2026-10-16:morning")["payload"]
        # The rebuilt summary replaces the unacknowledged original.
        assert _isolated.event_by_key("daily_brief:2026-10-16:morning")["state"] == "superseded"

    @pytest.mark.asyncio
    async def test_rebuilt_once_after_getting_to_know_you(self, _isolated, monkeypatch):
        await _deliver_day_one(_isolated, {})
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(10)), \
             patch.object(sync_status, "summary_snapshot", lambda: _snapshot()):
            assert scheduler._start_day_one_recompose() is None
            monkeypatch.setattr(scheduler, "_know_you_completed_at", lambda: time.time())
            assert await _run(scheduler._start_day_one_recompose()) is not None
            assert scheduler._start_day_one_recompose() is None
        assert _brief_keys(_isolated)[-1] == "daily_brief:2026-10-16:morning:rebuilt"

    @pytest.mark.asyncio
    async def test_day_one_publishes_at_most_one_rebuild_per_run(self, _isolated, monkeypatch):
        """Every brief is a "summary ready" moment. Day one: each run, plus one rebuild each."""
        await _deliver_day_one(_isolated, {"calendar": "unavailable", "messages": "unavailable"})
        with patch.object(B, "_sections", AsyncMock(return_value=READY)):
            # The morning: a source arrives, then Getting to know you ends, then
            # another source arrives. Ticks every 30 s across the first 15 minutes.
            states = {"calendar": "unavailable", "messages": "unavailable"}
            with patch.object(scheduler, "datetime", _clock_at(9, 5)), \
                 patch.object(sync_status, "summary_snapshot", lambda: _snapshot(**states)):
                for minute in range(30):
                    if minute == 4:
                        states.pop("calendar")
                    if minute == 8:
                        monkeypatch.setattr(scheduler, "_know_you_completed_at", lambda: time.time())
                    if minute == 12:
                        states.pop("messages")
                    await _run(scheduler._start_daily_brief() or scheduler._start_day_one_recompose())
            assert len(_brief_keys(_isolated)) == 2
            # The evening run gets its own one rebuild, and nothing more.
            monkeypatch.setattr(scheduler, "_know_you_completed_at", lambda: None)
            states = {"email": "unavailable"}
            with patch.object(scheduler, "datetime", _clock_at(20)), \
                 patch.object(sync_status, "summary_snapshot", lambda: _snapshot(**states)):
                for tick in range(30):
                    if tick == 3:
                        states.pop("email")
                    await _run(scheduler._start_daily_brief() or scheduler._start_day_one_recompose())
        assert _brief_keys(_isolated) == ["daily_brief:2026-10-16:morning",
                                          "daily_brief:2026-10-16:morning:rebuilt",
                                          "daily_brief:2026-10-16:evening",
                                          "daily_brief:2026-10-16:evening:rebuilt"]

    @pytest.mark.asyncio
    async def test_profile_finished_before_the_summary_needs_no_rebuild(self, _isolated, monkeypatch):
        monkeypatch.setattr(scheduler, "_know_you_completed_at", lambda: INSTALLED + 60)
        await _deliver_day_one(_isolated, {})
        with patch.object(scheduler, "datetime", _clock_at(10)), \
             patch.object(sync_status, "summary_snapshot", lambda: _snapshot()):
            assert scheduler._pending_recompose(scheduler.datetime.now()) is None

    @pytest.mark.asyncio
    async def test_failed_rebuild_counts_against_its_own_retries(self, _isolated):
        await _deliver_day_one(_isolated, {"calendar": "unavailable"})
        with patch.object(B, "_sections", AsyncMock(return_value={"FULL": "still syncing"})), \
             patch.object(scheduler, "datetime", _clock_at(10)), \
             patch.object(sync_status, "summary_snapshot", lambda: _snapshot()):
            await _run(scheduler._start_day_one_recompose())
        assert scheduler._brief_attempts == {(DAY, "morning:rebuilt"): 1}
        assert _brief_keys(_isolated) == ["daily_brief:2026-10-16:morning"]

    @pytest.mark.asyncio
    async def test_only_the_first_launch_day(self, _isolated):
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED - 86400))
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(9)), \
             patch.object(sync_status, "summary_snapshot", lambda: _snapshot(calendar="unavailable")):
            assert await _run(scheduler._start_daily_brief()) is not None
        assert _isolated.completion(scheduler._DAY_ONE_KEY) is None

    @pytest.mark.asyncio
    async def test_a_newer_run_wins_over_a_rebuild(self, _isolated):
        await _deliver_day_one(_isolated, {"calendar": "unavailable"})
        with patch.object(scheduler, "datetime", _clock_at(21)), \
             patch.object(sync_status, "summary_snapshot", lambda: _snapshot()):
            # The evening run is due: it reads every source fresh instead.
            assert scheduler._pending_recompose(scheduler.datetime.now()) is None
