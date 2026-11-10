"""Timers, alarms and sleep timers ring, repeat and survive a restart.

The real tools run through registry.run_tool on a real event loop, the way a
chat turn runs them. The notification and the media pause are stubs and the
store is a temp file: no osascript runs and nothing outside the test is read.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import time
from types import SimpleNamespace

import pytest

from service.tools import registry
from service.tools import timers_alarms as ta


@pytest.fixture
def fired(tmp_path, monkeypatch):
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(ta, "_STORE", tmp_path / "cache" / "timers.json")
    monkeypatch.setattr(ta, "_TIMERS", {})
    monkeypatch.setattr(ta, "_TASKS", {})
    monkeypatch.setattr(ta, "_notify", lambda title, body: events.append((title, body)))
    monkeypatch.setattr(ta, "_pause_media", lambda: events.append(("pause", "")))
    return events


def _saved() -> dict:
    return json.loads(ta._STORE.read_text(encoding="utf-8"))


async def _run(name: str, **args) -> str:
    return await registry.run_tool(registry.REGISTRY[name], args)


async def _until(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return bool(cond())


async def _settle() -> None:
    """Let every timer task that has rung finish its bookkeeping."""
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
             and getattr(t.get_coro(), "__name__", "") == "_fire"]
    done = [t for t in tasks if t.done()]
    await asyncio.sleep(0.05)
    assert all(not t.cancelled() and t.exception() is None for t in done)


def _entry(kind: str, late: float, **extra) -> dict:
    now = time.time()
    return {"kind": kind, "label": extra.pop("label", ""), "deadline": now - late,
            "created": now - late - 60, **extra}


def test_both_tools_run_on_the_event_loop_and_stay_the_modules_own():
    for name in ("set_timer", "manage_timers"):
        tool = registry.REGISTRY[name]
        assert inspect.iscoroutinefunction(tool.func), name
        assert getattr(ta, name) is tool.func and tool.func.__module__ == ta.__name__
        assert tool.category == "timer_write"


def test_a_timer_set_through_the_tool_rings_and_clears(fired):
    async def go():
        out = await _run("set_timer", duration="0.3s", label="tea")
        assert out == "Timer set for 0s (tea). I'll notify you when it's up."
        (tid,) = ta._TIMERS
        assert not ta._TASKS[tid].done() and tid in _saved()
        assert fired == []
        assert await _until(lambda: fired)
        await _settle()
        assert fired == [("tea", "Time's up")]
        assert ta._TIMERS == {} and ta._TASKS == {} and _saved() == {}
    asyncio.run(go())


def test_an_alarm_and_a_sleep_timer_ring_too(fired):
    async def go():
        soon = dt.datetime.now() + dt.timedelta(hours=2)
        out = await _run("set_timer", kind="alarm", time=soon.strftime("%H:%M"), label="Up")
        assert out.startswith("Alarm set for ")
        (tid,) = ta._TIMERS
        assert not ta._TASKS[tid].done()
        out = await _run("set_timer", kind="sleep", duration="0.2s")
        assert out == "Playback will pause in 0s."
        assert await _until(lambda: len(fired) == 2)
        await _settle()
        assert fired == [("pause", ""), ("Sleep timer", "Sleep timer — playback paused")]
        assert list(ta._TIMERS) == [tid]          # the alarm is still waiting
    asyncio.run(go())


def test_cancel_and_cancel_all_stop_a_running_timer(fired):
    async def go():
        await _run("set_timer", duration="0.3s", label="tea")
        (tid,) = ta._TIMERS
        task = ta._TASKS[tid]
        assert await _run("manage_timers", action="cancel", label="tea") == "Cancelled the timer 'tea'."
        await _run("set_timer", duration="0.3s", label="eggs")
        await _run("set_timer", duration="0.3s", label="rice")
        others = list(ta._TASKS.values())
        assert await _run("manage_timers", action="cancel_all") == "Cancelled 2 timers."
        await asyncio.sleep(0.6)
        assert task.cancelled() and all(t.cancelled() for t in others)
        assert fired == [] and ta._TIMERS == {} and ta._TASKS == {} and _saved() == {}
    asyncio.run(go())


def test_a_refused_timer_arms_nothing(fired):
    async def go():
        tool = registry.REGISTRY["set_timer"]
        result = await registry.run_tool(tool, {"duration": "soonish"})
        assert registry.not_done_receipt(tool, result), result
        assert ta._TIMERS == {} and ta._TASKS == {} and not ta._STORE.exists()
    asyncio.run(go())


def test_a_daily_alarm_goes_back_in_for_tomorrow(fired):
    async def go():
        first = time.time() + 0.2
        ta._TIMERS["a1"] = {"kind": "alarm", "label": "Up", "deadline": first,
                            "created": time.time(), "repeat": True}
        ta._arm("a1")
        assert await _until(lambda: fired)
        await _settle()
        assert fired == [("Up", "Alarm")]
        again = ta._TIMERS["a1"]
        at, nxt = dt.datetime.fromtimestamp(first), dt.datetime.fromtimestamp(again["deadline"])
        assert (nxt.hour, nxt.minute, nxt.second) == (at.hour, at.minute, 0)
        assert nxt.date() == at.date() + dt.timedelta(days=1)
        assert again["repeat"] is True and again["label"] == "Up"
        assert not ta._TASKS["a1"].done() and _saved()["a1"]["deadline"] == again["deadline"]
        # Cancelling a daily alarm ends it for good.
        waiting = ta._TASKS["a1"]
        assert await _run("manage_timers", action="cancel", label="up") == "Cancelled the timer 'Up'."
        await asyncio.sleep(0.05)
        assert waiting.cancelled() and ta._TIMERS == {} and _saved() == {}
        assert fired == [("Up", "Alarm")]
    asyncio.run(go())


def test_a_one_time_alarm_does_not_come_back(fired):
    async def go():
        ta._TIMERS["a1"] = {"kind": "alarm", "label": "", "deadline": time.time() + 0.1,
                            "created": time.time(), "repeat": False}
        ta._arm("a1")
        assert await _until(lambda: fired)
        await _settle()
        assert fired == [("Alarm", "Alarm")] and ta._TIMERS == {} and ta._TASKS == {}
    asyncio.run(go())


def test_next_daily_keeps_the_clock_time():
    seven = dt.datetime(2026, 3, 2, 7, 0)
    before = dt.datetime(2026, 3, 5, 6, 30).timestamp()
    after = dt.datetime(2026, 3, 5, 7, 0, 1).timestamp()
    assert ta._next_daily(seven.timestamp(), before) == dt.datetime(2026, 3, 5, 7, 0).timestamp()
    assert ta._next_daily(seven.timestamp(), after) == dt.datetime(2026, 3, 6, 7, 0).timestamp()
    # Across a clock change the wall time holds (whatever zone the test runs in).
    for day in (dt.datetime(2026, 3, 7, 7, 0), dt.datetime(2026, 10, 31, 7, 0)):
        nxt = dt.datetime.fromtimestamp(ta._next_daily(day.timestamp(), day.timestamp() + 1))
        assert (nxt.date(), nxt.hour, nxt.minute) == (day.date() + dt.timedelta(days=1), 7, 0)


def test_startup_arms_what_is_still_due_and_drops_the_rest_silently(fired):
    saved = {
        "future": _entry("timer", -3600, label="bread"),
        "stale": _entry("timer", 2 * 3600, label="old pasta"),
        "ancient": _entry("alarm", 40 * 86400, label="old alarm"),
        "latesleep": _entry("sleep", 120, label="Sleep timer"),
        "recent": _entry("timer", 30, label="just missed"),
        "daily": _entry("alarm", 2 * 86400 + 60, label="Up", repeat=True),
        "nodeadline": {"kind": "timer", "label": "x"},
        "textdeadline": {"kind": "timer", "label": "x", "deadline": "soon"},
        "booldeadline": {"kind": "timer", "label": "x", "deadline": True},
        "nokind": {"label": "x", "deadline": time.time() + 60},
        "oddkind": {"kind": "gong", "label": "x", "deadline": time.time() + 60},
    }
    ta._STORE.parent.mkdir(parents=True)
    ta._STORE.write_text(json.dumps(saved), encoding="utf-8")

    async def go():
        ta._load()                       # as at import: filled, nothing armed
        assert set(ta._TIMERS) == set(saved) and ta._TASKS == {}
        ta.rearm_all()
        assert set(ta._TIMERS) == {"future", "recent", "daily"}
        assert set(ta._TASKS) == {"future", "recent", "daily"}
        assert ta._TIMERS["daily"]["deadline"] > time.time()
        up = dt.datetime.fromtimestamp(ta._TIMERS["daily"]["deadline"])
        was = dt.datetime.fromtimestamp(saved["daily"]["deadline"])
        assert (up.hour, up.minute) == (was.hour, was.minute)
        assert set(_saved()) == {"future", "recent", "daily"}
        assert await _until(lambda: fired)
        await _settle()
        # The one that came due while Wisp restarted rings once; nothing else does.
        assert fired == [("just missed", "Time's up")]
        assert set(ta._TIMERS) == {"future", "daily"}
        waiting = dict(ta._TASKS)
        assert set(waiting) == {"future", "daily"} and not any(t.done() for t in waiting.values())
        ta.rearm_all()                   # a second call leaves live timers alone
        assert ta._TASKS == waiting
        listing = await _run("manage_timers", action="list")
        assert "bread" in listing and "old pasta" not in listing
    asyncio.run(go())


def test_a_daily_alarm_just_missed_at_startup_rings_then_rolls_forward(fired):
    async def go():
        ta._TIMERS["a1"] = _entry("alarm", 20, label="Up", repeat=True)
        ta.rearm_all()
        assert await _until(lambda: fired)
        await _settle()
        assert fired == [("Up", "Alarm")]
        assert ta._TIMERS["a1"]["deadline"] > time.time() and not ta._TASKS["a1"].done()
    asyncio.run(go())


def test_rearm_without_an_event_loop_changes_nothing(fired):
    ta._TIMERS["stale"] = _entry("timer", 7200)
    ta._TIMERS["future"] = _entry("timer", -600)
    before = json.loads(json.dumps(ta._TIMERS))
    ta.rearm_all()
    assert ta._TIMERS == before and ta._TASKS == {} and not ta._STORE.exists()


def test_the_service_arms_saved_timers_at_startup():
    from service import main

    src = inspect.getsource(main.lifespan)
    assert src.count("timers_alarms.rearm_all()") == 1
    assert src.index("timers_alarms.rearm_all()") < src.index("yield")


class _Aio:
    """asyncio, with `sleep` swapped out for this module only."""

    def __init__(self, sleep):
        self.sleep = sleep

    def __getattr__(self, name):
        return getattr(asyncio, name)


def _asleep_mac(monkeypatch, slept: float):
    """A clock that jumps `slept` seconds across the first sleep, as when the lid shuts."""
    clock = {"now": 1_800_000_000.0}
    naps: list[float] = []

    async def nap(seconds):
        naps.append(seconds)
        clock["now"] += slept if len(naps) == 1 else seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(ta, "time", SimpleNamespace(time=lambda: clock["now"]))
    monkeypatch.setattr(ta, "asyncio", _Aio(nap))
    return clock, naps


def test_an_alarm_rings_on_wake_instead_of_hours_late(fired, monkeypatch):
    clock, naps = _asleep_mac(monkeypatch, slept=8 * 3600)

    async def go():
        ta._TIMERS["a1"] = {"kind": "alarm", "label": "Up", "deadline": clock["now"] + 3600,
                            "created": clock["now"]}
        ta._arm("a1")
        assert await _until(lambda: fired)
    asyncio.run(go())
    # One short nap, then the wall clock said it was past time. Not one 3600 s sleep.
    assert naps == [30.0] and fired == [("Up", "Alarm")] and ta._TIMERS == {}


def test_a_sleep_timer_the_mac_slept_through_pauses_nothing(fired, monkeypatch):
    clock, naps = _asleep_mac(monkeypatch, slept=8 * 3600)

    async def go():
        ta._TIMERS["s1"] = {"kind": "sleep", "label": "Sleep timer",
                            "deadline": clock["now"] + 1200, "created": clock["now"]}
        ta._arm("s1")
        assert await _until(lambda: not ta._TIMERS)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert naps == [30.0] and fired == [] and ta._TASKS == {}


def test_nothing_rings_or_is_written_back_after_a_reset_begins(fired, monkeypatch):
    from service import wisp_reset

    async def go():
        ta._TIMERS["t1"] = {"kind": "timer", "label": "tea", "deadline": time.time() + 0.1,
                            "created": time.time()}
        ta._arm("t1")
        monkeypatch.setattr(wisp_reset.state, "sealed", True)
        task = ta._TASKS["t1"]
        assert await _until(task.done)
        assert task.exception() is None
    asyncio.run(go())
    assert fired == [] and not ta._STORE.exists()
