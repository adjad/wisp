"""The first Daily Summary is composed as soon as onboarding is answered.

When the app reports that first-run onboarding reached its last step, the
scheduler (not the endpoint) composes the first Brief on its next tick:

  * the 15-minute first-launch permission grace no longer holds it, because a
    source still never answered was skipped on purpose;
  * on the first launch day only, the morning run is due before its hour, from
    EARLY_FIRST_BRIEF_HOUR on, and the morning hour then finds it delivered.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-brief-onboarding-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from service import config, idle                                # noqa: E402
from service.assistant import brief as B                        # noqa: E402
from service.assistant import scheduler                         # noqa: E402
from service.assistant.hub import hub                           # noqa: E402
from service.assistant.store import FIRST_LAUNCH_KEY            # noqa: E402

READY = {"FULL": "Your day.", "TODAY": "Today: nothing.",
         "MESSAGES": "No new messages.", "READY": "1"}
DAY = date(2026, 10, 16)
# Installed in the small hours of the test day, so "first launch day" holds at 06:30.
INSTALLED = datetime(2026, 10, 16, 5, 30).timestamp()
MARKER = scheduler.ONBOARDING_ANSWERED_KEY


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


def _answer_onboarding(store) -> None:
    store.record_completion(MARKER, repr(time.time()))


def _brief_keys(store) -> list[str]:
    with store._lock:
        return [r[0] for r in store._db.execute(
            "SELECT dedupe_key FROM assistant_events WHERE kind='daily_brief' ORDER BY created_at,id")]


async def _run(task):
    if task is not None:
        await task
    return task


class TestGrace:
    @pytest.mark.asyncio
    async def test_marker_releases_a_never_answered_messages_source(self, _isolated):
        with patch("service.tools.imessage_tools.messages_sync_state", lambda: "unavailable"), \
             patch("service.tools.imessage_tools.messages_unavailable_kind", lambda: "full_disk_access"):
            assert scheduler._awaiting_first_permission(time.time())
            _answer_onboarding(_isolated)
            assert not scheduler._awaiting_first_permission(time.time())

    @pytest.mark.asyncio
    async def test_without_the_marker_the_run_still_waits(self, _isolated):
        with patch("service.tools.imessage_tools.messages_sync_state", lambda: "unavailable"), \
             patch("service.tools.imessage_tools.messages_unavailable_kind", lambda: "full_disk_access"), \
             patch.object(B, "run_scheduled_brief", AsyncMock(return_value=True)) as run, \
             patch.object(scheduler, "datetime", _clock_at(9)):
            assert await _run(scheduler._start_daily_brief()) is None
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_marker_lets_the_run_start_inside_the_grace(self, _isolated):
        _answer_onboarding(_isolated)
        with patch("service.tools.imessage_tools.messages_sync_state", lambda: "unavailable"), \
             patch("service.tools.imessage_tools.messages_unavailable_kind", lambda: "full_disk_access"), \
             patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(9)):
            assert await _run(scheduler._start_daily_brief()) is not None
        assert _brief_keys(_isolated) == ["daily_brief:2026-10-16:morning"]


class TestEarlyFirstBrief:
    def _pending(self, hour: int, minute: int = 0, day: int = 16):
        return scheduler._pending_slot(_clock_at(hour, minute, day).now())

    def test_first_day_before_the_morning_hour_is_due_early(self, _isolated):
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
        _answer_onboarding(_isolated)
        assert self._pending(6, 30) == (DAY, "morning")
        assert self._pending(scheduler.EARLY_FIRST_BRIEF_HOUR) == (DAY, "morning")

    def test_not_in_the_small_hours(self, _isolated):
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
        _answer_onboarding(_isolated)
        assert self._pending(4, 0) is None

    def test_not_without_the_marker(self, _isolated):
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
        assert self._pending(6, 30) is None

    def test_not_on_a_later_day(self, _isolated):
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
        _answer_onboarding(_isolated)
        assert self._pending(6, 30, day=17) is None

    def test_not_for_an_upgraded_install(self, _isolated):
        _isolated.record_completion(FIRST_LAUNCH_KEY, "0")
        _answer_onboarding(_isolated)
        assert self._pending(6, 30) is None

    @pytest.mark.asyncio
    async def test_no_second_brief_at_the_morning_hour(self, _isolated):
        _isolated.record_completion(FIRST_LAUNCH_KEY, repr(INSTALLED))
        _answer_onboarding(_isolated)
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(6, 30)):
            assert await _run(scheduler._start_daily_brief()) is not None
        assert _brief_keys(_isolated) == ["daily_brief:2026-10-16:morning"]
        assert self._pending(6, 31) is None
        assert self._pending(8, 5) is None
        with patch.object(B, "_sections", AsyncMock(return_value=READY)), \
             patch.object(scheduler, "datetime", _clock_at(8, 5)):
            assert await _run(scheduler._start_daily_brief()) is None
        assert _brief_keys(_isolated) == ["daily_brief:2026-10-16:morning"]
        # The evening run is unaffected.
        assert self._pending(20, 0) == (DAY, "evening")


class TestEndpoint:
    def test_records_the_marker_once(self, _isolated, monkeypatch):
        from fastapi.testclient import TestClient
        from service import main
        monkeypatch.setattr(main, "assistant_store", _isolated)
        assert _isolated.completion(MARKER) is None
        client = TestClient(main.app)
        assert client.post("/assistant/onboarding/brief_ready").json() == {"ok": True}
        first = _isolated.completion(MARKER)
        assert first is not None and float(first) > 0
        assert client.post("/assistant/onboarding/brief_ready").json() == {"ok": True}
        assert _isolated.completion(MARKER) == first
        # It records only: the scheduler composes the Brief, never the endpoint.
        assert _brief_keys(_isolated) == []
