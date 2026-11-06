"""Catch the Brief up when Wisp comes back after a long absence.

The proactive Brief (cards, to-dos, the mail and texts overviews) is rewritten
in full only by the hourly idle refresh (cycle.idle_refresh), and that waits
until the user has gone AWAY again. So after Wisp was quit, or Save memory
parked the chat model, the person who just came back read the Brief as it
was when they left, until they next walked away. This module regenerates it
once, as they come back.

IMPORTANT rules:
- "Coming back" is one of: the service starting (the app relaunched), the chat
  model resident again after Save memory parked it (a turn or intent loaded it,
  never a batch window), or the app's notch opening (POST /assistant/return).
- Stale means the MODEL last read the sources (state "model_as_of")
  STALE_AFTER_S ago or on an earlier local day. Not "as_of": while the model is
  parked the ordinary cycle still runs every few minutes by rule and keeps
  as_of current. A Brief that was never built is the ordinary cycle's job.
- Arming never loads a model. The catch-up starts only while the chat model is
  already resident and no batch window is open, so it can't defeat Save memory:
  a launch or an unpark just waits for someone to open Wisp, and opening Ask
  already prewarms (memory_saver.prewarm). Memory pressure keeps it waiting too.
- It runs from cycle.schedule_tick, so it is single-flight with the ordinary
  cycle and the gist calls, never starts while a turn is in flight, and stands
  down when one starts (it stays armed and tries again). It also waits until
  the user's arrival (the arm, the notch opening, an intent, a turn) is
  SETTLE_S old: someone who just opened Wisp is about to ask something, and a
  cancelled request may not stop the engine generating. schedule_tick also holds
  it until the chat has gone quiet, and for the minimum gap after a build
  (service/brief_pace.py). Its model calls go through cycle.resident_reply, under
  the Brief's Save power cap.
- While it runs it keeps the model resident (memory_saver.hold), at most
  RUN_MAX_S for the cycle and the gists together, so a park between two calls
  can't leave half the Brief by rule. A cycle that fell back to rules anyway
  doesn't count. Whatever ends a run, a still-owed catch-up backs off.
- By day the scheduled builds stand in for this catch-up (brief_pace.automatic_plan,
  cycle._build_tick): what is owed is settled by the next one, and the Brief says
  "updating" only when one may run. This run itself is for quiet hours, a new
  install, and a Brief the model hasn't read for a day.
- A finished catch-up counts as the hour's idle refresh, and tells the app
  "changed" so an open notch shows it at once.
- While it is running, or for UPDATING_FOR_S after the notch opened on a stale
  Brief, workspace.proactive says `updating`, so the Brief says "Updating your
  Brief…" instead of passing the old cards off as current. Not while a chat holds
  the catch-up back: nothing is updating then, and it would look as if it were.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# Older than this, the Brief is caught up on return.
STALE_AFTER_S = 45 * 60
# How long the Brief says "Updating…" after the notch opened on a stale Brief,
# if the catch-up hasn't been able to start (the model is still loading).
UPDATING_FOR_S = 3 * 60
# The longest a catch-up may hold the model (cycle and gists together).
RUN_MAX_S = 8 * 60
# How long after the user arrives before a catch-up may start.
SETTLE_S = 45
# Waits before trying again: the model wasn't resident, or a turn started.
WAIT_S = 60
YIELD_S = 120

_armed: dict | None = None        # {"reason", "at"}; None when nothing is owed
_opened_at: float | None = None   # when the notch last opened on a stale Brief
_running = False
_retry_at = 0.0
_launch_checked = False


def _log(text: str) -> None:
    print(f"[proactive] return refresh: {text}", flush=True)


def _day(ts: float, tz: str) -> str:
    try:
        return datetime.fromtimestamp(ts, ZoneInfo(tz)).date().isoformat()
    except Exception:  # noqa: BLE001 — an unknown zone: the Mac's own
        return datetime.fromtimestamp(ts).date().isoformat()


def _stamp(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_stale(now: float, state: dict) -> bool:
    """The model last read the Brief's sources STALE_AFTER_S ago or on an
    earlier local day. Never built (no as_of) is not stale: the ordinary cycle
    builds it. Built but never by the model (or before model_as_of existed) is."""
    if _stamp(state.get("as_of")) is None:
        return False
    as_of = _stamp(state.get("model_as_of"))
    if as_of is None:
        return True
    if now - as_of >= STALE_AFTER_S:
        return True
    tz = state.get("timezone") or "UTC"
    return _day(now, tz) != _day(float(as_of), tz)


def _store(store=None):
    if store is not None:
        return store
    from service.proactive.store import store as default_store
    return default_store()


def arm(reason: str, *, now: float | None = None, store=None) -> bool:
    """Owe a catch-up when the Brief is stale. True when one is owed now
    (already owed counts). Never loads anything."""
    global _armed, _opened_at
    now = time.time() if now is None else now
    try:
        stale = is_stale(now, _store(store).state())
    except Exception:  # noqa: BLE001 — an unreadable store owes nothing
        return False
    if not stale:
        return _armed is not None
    if _armed is None:
        _armed = {"reason": reason, "at": now}
        _log(f"owed ({reason})")
    if reason == "opened":
        _opened_at = now
    return True


def arm_on_launch(*, now: float | None = None, store=None) -> bool:
    """The first scheduler tick after the service started."""
    global _launch_checked
    if _launch_checked:
        return False
    _launch_checked = True
    return arm("launch", now=now, store=store)


def pending() -> bool:
    return _armed is not None


def due(now: float | None = None) -> bool:
    """Owed, not running, and not backing off."""
    now = time.time() if now is None else now
    return _armed is not None and not _running and now >= _retry_at


def updating(now: float | None = None) -> bool:
    """What workspace.proactive says as `updating`."""
    if _running:
        return True
    now = time.time() if now is None else now
    if _armed is None or _opened_at is None or not 0 <= now - _opened_at < UPDATING_FOR_S:
        return False
    from service import brief_pace
    if brief_pace.chat_recent():
        return False
    try:
        return brief_pace.automatic_plan(now, _store().state()) != "light"
    except Exception:  # noqa: BLE001 — unreadable: as if a build may run
        return True


def status() -> dict:
    return {"owed": _armed is not None, "reason": (_armed or {}).get("reason"), "running": _running,
            "updating": updating()}


def _arrived(now: float) -> float:
    """Seconds since the user last showed up: the arm, the notch opening, an
    intent (opening Ask) or a turn, whichever is latest."""
    ages = [now - t for t in ((_armed or {}).get("at"), _opened_at) if t is not None]
    try:
        from service import idle, memory_saver
        mono = time.monotonic()
        ages += [mono - t for t in (memory_saver._state.last_intent, idle.last_turn()) if t is not None]
    except Exception:  # noqa: BLE001 — unknown: the wall-clock marks decide
        pass
    return min(ages) if ages else float("inf")


async def _model_ready(*, window_ok: bool = False) -> bool:
    """The chat model is resident, no batch window is open, and memory isn't
    tight: the only time a catch-up may start. Never loads anything."""
    from service import memory_saver
    if (not window_ok and memory_saver.batch_window_open()) or memory_saver.pressure_level() in (
            memory_saver.WARN, memory_saver.CRITICAL):
        return False
    from service.assistant.brief import _c
    from service.config import role_to_model
    from service.inference.remote_policy import remote_only_profile
    if remote_only_profile() is not None:
        return True
    try:
        return role_to_model("fast") in set(await _c().loaded_models())
    except Exception:  # noqa: BLE001 — an unreachable engine: not now
        return False


async def model_ready() -> bool:
    """For the day's builds: resident (a batch window's model counts) and memory
    isn't tight. Never loads anything."""
    return await _model_ready(window_ok=True)


@contextlib.contextmanager
def building():
    """A scheduled build is running: the Brief says so, and the model stays resident."""
    global _running
    from service.memory_saver import hold
    _running = True
    try:
        with hold():
            yield
    finally:
        _running = False


def caught_up() -> None:
    """A scheduled build read everything; no catch-up is owed."""
    global _armed, _opened_at
    _armed, _opened_at = None, None


async def announce() -> None:
    try:
        await _announce()
    except Exception:  # noqa: BLE001 — the app's next read shows it anyway
        pass


async def _announce() -> None:
    from service.assistant.hub import hub
    await hub.publish({"type": "changed"}, durable=False)


async def run(*, now: float | None = None, store=None, cycle, gists, ready=None, announce=None,
              hold=None, arrived=None) -> str:
    """One catch-up attempt. Returns:

    - "idle": nothing owed, or backing off;
    - "fresh": something else (the ordinary cycle, on the model) caught the
      Brief up since it was owed; no longer owed;
    - "settling": the user arrived under SETTLE_S ago; tried again after;
    - "waiting": the model isn't resident (or a batch window / pressure);
    - "yielded": a turn started, it fell back to rules, or it ran past
      RUN_MAX_S; still owed;
    - "ran": the Brief was regenerated, and is no longer owed."""
    global _armed, _running, _retry_at, _opened_at
    now = time.time() if now is None else now
    if not due(now):
        return "idle"
    store = _store(store)
    try:
        stale = is_stale(now, store.state())
    except Exception:  # noqa: BLE001 — unreadable: try the catch-up as owed
        stale = True
    if not stale:
        # While it settled or waited, a model-backed ordinary cycle already
        # brought the Brief up to date: a forced one now would only repeat it.
        _armed, _opened_at = None, None
        _log("no longer owed: the Brief was caught up meanwhile")
        return "fresh"
    since = (arrived or _arrived)(now)
    if since < SETTLE_S:
        _retry_at = now + (SETTLE_S - since)
        return "settling"
    if not await (ready or _model_ready)():
        _retry_at = now + WAIT_S
        return "waiting"
    if hold is None:
        from service.memory_saver import hold
    _running = True
    _log(f"regenerating the Brief ({_armed['reason']})")
    started = time.monotonic()
    deadline = started + RUN_MAX_S
    try:
        with hold():
            try:
                result = await asyncio.wait_for(cycle(force=True), RUN_MAX_S)
            except asyncio.TimeoutError:
                result = {"status": "timeout"}
            if (not isinstance(result, dict) or result.get("status") != "done"
                    or result.get("model") is False):
                return "yielded"
            store.save_state(idle_refresh_at=now)
            try:
                await asyncio.wait_for(gists(), max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                _log("the overviews ran out of time; they keep their last text")
            except Exception:  # noqa: BLE001 — the cards are fresh; the overviews keep their text
                import traceback
                traceback.print_exc()
        from service.proactive.cycle import refresh_inputs
        store.save_state(idle_refresh_inputs=refresh_inputs(store, now))
        _armed, _opened_at = None, None
    finally:
        _running = False
        if _armed is not None:
            # Whatever ended it (a turn, rules, a timeout, an error), back off
            # from when it ENDED: a run longer than YIELD_S would otherwise end
            # with its retry already due, and force cycle after cycle.
            ended = now + (time.monotonic() - started)
            _retry_at = max(_retry_at, ended + YIELD_S)
    try:
        await (announce or _announce)()
    except Exception:  # noqa: BLE001 — the app's next read shows it anyway
        pass
    return "ran"


def _reset_for_tests() -> None:
    global _armed, _opened_at, _running, _retry_at, _launch_checked
    _armed, _opened_at, _running, _retry_at, _launch_checked = None, None, False, 0.0, False
