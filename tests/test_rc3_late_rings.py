"""A timer or alarm the Mac slept through for hours does not ring on wake.

Owner decision 2026-11-10. Inside a running backend `_fire` now applies the
same late-ring limit as startup (`rearm_all`): hours late is dropped silently
(a one-shot leaves the store; a daily alarm stays armed for its next time),
while a ring only a little late, a short nap or a slow wake, still rings.

The clock is fake: it jumps across the first sleep, the way it does when the
lid shuts, and then stands still, so a re-armed alarm just waits. The
notification and the media pause are stubs and the store is a temp file.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest

from service.tools import registry
from service.tools import timers_alarms as ta

T0 = 1_800_000_000.0


@pytest.fixture
def fired(tmp_path, monkeypatch):
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(ta, "_STORE", tmp_path / "cache" / "timers.json")
    monkeypatch.setattr(ta, "_TIMERS", {})
    monkeypatch.setattr(ta, "_TASKS", {})
    monkeypatch.setattr(ta, "_notify", lambda title, body: events.append((title, body)))
    monkeypatch.setattr(ta, "_pause_media", lambda: events.append(("pause", "")))
    return events


class _Aio:
    """asyncio, with `sleep` swapped out for this module only."""

    def __init__(self, sleep):
        self.sleep = sleep

    def __getattr__(self, name):
        return getattr(asyncio, name)


def _lid(monkeypatch, slept: float) -> dict:
    """The wall clock jumps `slept` seconds across the first sleep, then holds."""
    clock = {"now": T0, "naps": 0}

    async def nap(seconds):
        clock["naps"] += 1
        if clock["naps"] == 1:
            clock["now"] += slept
        await asyncio.sleep(0.01)

    monkeypatch.setattr(ta, "time", SimpleNamespace(time=lambda: clock["now"]))
    monkeypatch.setattr(ta, "asyncio", _Aio(nap))
    return clock


def _saved() -> dict:
    return json.loads(ta._STORE.read_text(encoding="utf-8")) if ta._STORE.exists() else {}


async def _until(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return bool(cond())


async def _listing() -> str:
    return await registry.run_tool(registry.REGISTRY["manage_timers"], {"action": "list"})


def _cancel_all_tasks() -> None:
    for t in ta._TASKS.values():
        t.cancel()


def test_grace_values_are_one_rule_for_startup_and_wake():
    assert ta._LATE_GRACE == 300.0 and ta._SLEEP_LATE_GRACE == 60.0
    assert not ta._too_late("timer", 300.0) and ta._too_late("timer", 300.1)
    assert not ta._too_late("alarm", 299.0) and ta._too_late("alarm", 3 * 3600)
    assert not ta._too_late("sleep", 60.0) and ta._too_late("sleep", 61.0)


def test_a_timer_slept_through_for_hours_does_not_ring_and_is_removed(fired, monkeypatch):
    clock = _lid(monkeypatch, slept=5 * 3600)

    async def go():
        ta._TIMERS["t1"] = {"kind": "timer", "label": "pasta", "deadline": T0 + 600,
                            "created": T0}
        ta._save()
        ta._arm("t1")
        task = ta._TASKS["t1"]
        assert await _until(task.done)
        assert task.exception() is None
        assert fired == [] and ta._TIMERS == {} and ta._TASKS == {} and _saved() == {}
        assert await _listing() == "No timers or alarms are running."
    asyncio.run(go())
    assert clock["naps"] == 1


def test_a_one_time_alarm_slept_through_does_not_ring(fired, monkeypatch):
    _lid(monkeypatch, slept=7 * 3600)

    async def go():
        ta._TIMERS["a1"] = {"kind": "alarm", "label": "Up", "deadline": T0 + 3600,
                            "created": T0, "repeat": False}
        ta._save()
        ta._arm("a1")
        assert await _until(ta._TASKS["a1"].done)
        assert fired == [] and ta._TIMERS == {} and _saved() == {}
        assert "Up" not in await _listing()
    asyncio.run(go())


def test_a_daily_alarm_slept_through_skips_to_its_next_time_and_stays_armed(fired, monkeypatch):
    clock = _lid(monkeypatch, slept=7 * 3600)
    first = T0 + 3600

    async def go():
        ta._TIMERS["d1"] = {"kind": "alarm", "label": "Up", "deadline": first,
                            "created": T0, "repeat": True}
        ta._save()
        ta._arm("d1")
        old = ta._TASKS["d1"]
        assert await _until(old.done)
        assert fired == []
        again = ta._TIMERS["d1"]
        assert again["deadline"] > clock["now"]
        at, nxt = dt.datetime.fromtimestamp(first), dt.datetime.fromtimestamp(again["deadline"])
        assert (nxt.hour, nxt.minute) == (at.hour, at.minute)
        assert again["repeat"] is True and again["label"] == "Up"
        assert _saved()["d1"]["deadline"] == again["deadline"]
        armed = ta._TASKS["d1"]
        assert armed is not old and not armed.done()
        assert "Alarm 'Up'" in await _listing()
        await asyncio.sleep(0.05)
        assert fired == []            # waiting for tomorrow, not ringing late
        _cancel_all_tasks()
    asyncio.run(go())


def test_late_by_less_than_the_grace_still_rings(fired, monkeypatch):
    # Deadline 10 minutes out, asleep 14 minutes: 4 minutes late, under 5.
    _lid(monkeypatch, slept=14 * 60)

    async def go():
        ta._TIMERS["t1"] = {"kind": "timer", "label": "tea", "deadline": T0 + 600, "created": T0}
        ta._TIMERS["a1"] = {"kind": "alarm", "label": "Up", "deadline": T0 + 600,
                            "created": T0, "repeat": True}
        ta._arm("t1")
        ta._arm("a1")
        assert await _until(lambda: len(fired) == 2)
        await asyncio.sleep(0.05)
        assert sorted(fired) == [("Up", "Alarm"), ("tea", "Time's up")]
        assert set(ta._TIMERS) == {"a1"} and ta._TIMERS["a1"]["deadline"] > T0 + 600
        _cancel_all_tasks()
    asyncio.run(go())


def test_a_sleep_timer_a_little_late_pauses_but_past_its_grace_does_not(fired, monkeypatch):
    _lid(monkeypatch, slept=600 + 30)

    async def go():
        ta._TIMERS["s1"] = {"kind": "sleep", "label": "Sleep timer", "deadline": T0 + 600,
                            "created": T0}
        ta._arm("s1")
        assert await _until(lambda: fired)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert fired == [("pause", ""), ("Sleep timer", "Sleep timer — playback paused")]

    fired.clear()
    _lid(monkeypatch, slept=600 + 120)

    async def go2():
        ta._TIMERS["s2"] = {"kind": "sleep", "label": "Sleep timer", "deadline": T0 + 600,
                            "created": T0}
        ta._arm("s2")
        assert await _until(ta._TASKS["s2"].done)
    asyncio.run(go2())
    assert fired == [] and ta._TIMERS == {}


def test_a_timer_cancelled_while_the_mac_slept_stays_silent(fired, monkeypatch):
    clock = {"now": T0}
    gate = asyncio.Event

    async def go():
        wake = gate()

        async def nap(seconds):
            await wake.wait()
            clock["now"] += 2 * 3600

        monkeypatch.setattr(ta, "time", SimpleNamespace(time=lambda: clock["now"]))
        monkeypatch.setattr(ta, "asyncio", _Aio(nap))
        ta._TIMERS["t1"] = {"kind": "timer", "label": "tea", "deadline": T0 + 60, "created": T0}
        ta._arm("t1")
        await asyncio.sleep(0.02)
        out = await registry.run_tool(registry.REGISTRY["manage_timers"],
                                      {"action": "cancel", "label": "tea"})
        assert out == "Cancelled the timer 'tea'."
        wake.set()
        await asyncio.sleep(0.05)
        assert fired == [] and ta._TIMERS == {} and ta._TASKS == {}
    asyncio.run(go())


def test_re_arming_never_rings_twice_and_startup_after_a_wake_drop_adds_nothing(fired, monkeypatch):
    _lid(monkeypatch, slept=3 * 60)

    async def go():
        ta._TIMERS["t1"] = {"kind": "timer", "label": "tea", "deadline": T0 + 60, "created": T0}
        ta._arm("t1")
        ta._arm("t1")                 # the first task is cancelled, one rings
        assert await _until(lambda: fired)
        await asyncio.sleep(0.05)
        assert fired == [("tea", "Time's up")] and ta._TIMERS == {}
        ta.rearm_all()
        await asyncio.sleep(0.05)
        assert fired == [("tea", "Time's up")] and ta._TASKS == {}
    asyncio.run(go())
