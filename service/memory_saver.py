"""Save memory: give back the resident chat model's memory while Wisp isn't being used.

The chat model (Gemma E4B, every text role) takes 5.5 GB of wired memory that
macOS can't compress or swap, so other apps get squeezed instead. Measured on
the M5 Pro, 2026-10-30, with Gemma the only model on the engine:

    situation                                   first token
    resident, used in the last 5 minutes        0.11-0.12 s
    resident, GPU parked after 330 s idle       0.22-0.26 s
    resident, GPU parked after 600 s idle       0.70 s
    unloaded (4.3 s load + 1.56 s first call)   ~5.9 s

The engine's GPU keep-warm (a tiny kernel every 0.5 s for 5 minutes after a
request) cost at most ~0.08 W there, so it is left alone: memory is the only
reason to unload.

IMPORTANT rules:
- Only keep-warm models (the chat model) are parked here. Every other model
  keeps the plain idle timer in service/idle_unloader.py.
- Never unload while a generation runs on the model or a user's turn is in
  flight. Parking never refuses a turn: a turn loads the model on demand, as it
  always has ("Loading model… (Ns)").
- Background model work is resident-only (mail_gists._resident_reply,
  mail_memory's _stand_down, memory capture, the behavior phraser all stand down
  when the model isn't loaded), so parking pauses it with no change to any job.
  A batch window loads the model about once an hour so that work catches up,
  then parks it again. Background calls never extend a park timer: only the
  user's turns and intent do.
- Memory pressure (macOS warn or critical) parks after PRESSURE_GRACE_S and
  holds batch windows off until it eases.
- Intent (the app opening Ask or the Wisp window, the Ask hotkeys) loads the
  model early so the reload hides behind typing, and holds it INTENT_HOLD_S.
- The "Save memory" switch (on by default) turns parking and batch windows off;
  the chat model then stays resident as before.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from service import idle
from service.paths import MOE_DIR

log = logging.getLogger(__name__)

# No turn for this long parks the model.
PARK_AFTER_S = 30 * 60
# Intent without a turn keeps the model this long.
INTENT_HOLD_S = 5 * 60
# Under memory pressure, park once the user has been quiet this long.
PRESSURE_GRACE_S = 2 * 60
# While parked, a batch window opens at most this often.
BATCH_EVERY_S = 60 * 60
# A window stays open at least BATCH_MIN_S, closes once background calls have
# been quiet for BATCH_QUIET_S, and never stays open past BATCH_MAX_S. The minimum
# outlasts the longest wait a job takes after a "not loaded" stand-down (the
# proactive cycle's IDLE_GAP_S of 300 s, plus the scheduler's 30 s tick), so every
# job gets its turn; mail_gists and the mail memory walk retry after 60 s.
BATCH_MIN_S = 6 * 60
BATCH_QUIET_S = 2 * 60
BATCH_MAX_S = 15 * 60

SETTINGS_PATH = MOE_DIR / "memory_saver" / "settings.json"
_KEY = "save_memory"

KEEP, PARK, OPEN_WINDOW, CLOSE_WINDOW, LEAVE = "keep", "park", "open_window", "close_window", "leave"


# --- the switch -------------------------------------------------------------------

def save_memory(path: Path | None = None) -> bool:
    """The "Save memory" switch. On unless the user turned it off."""
    path = SETTINGS_PATH if path is None else path
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or unreadable means the default
        return True
    return not (isinstance(data, dict) and data.get(_KEY) is False)


def set_save_memory(on: bool, path: Path | None = None) -> bool:
    if type(on) is not bool:
        raise ValueError("save_memory must be true or false")
    path = SETTINGS_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({_KEY: on}, f)
    tmp.replace(path)
    if not on:
        # Off means resident as before: a parked model comes back on the next tick.
        _state.window_started = None
    return on


# --- memory pressure --------------------------------------------------------------

NORMAL, WARN, CRITICAL, UNKNOWN = "normal", "warn", "critical", "unknown"
_LEVELS = {1: NORMAL, 2: WARN, 4: CRITICAL}
_libc = None


def pressure_level() -> str:
    """macOS's memory pressure (kern.memorystatus_vm_pressure_level): normal,
    warn, critical, or unknown when it can't be read (unknown never parks)."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if _libc.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(value),
                              ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
            return UNKNOWN
        return _LEVELS.get(value.value, UNKNOWN)
    except Exception:  # noqa: BLE001 — not macOS, or no such sysctl
        return UNKNOWN


# --- state ------------------------------------------------------------------------

@dataclass
class _State:
    started: float
    parked: bool = False                 # Wisp unloaded the chat model (here, or the notch's X)
    parked_at: float | None = None
    last_intent: float | None = None
    window_started: float | None = None  # a batch window is open
    last_window: float | None = None
    last_reason: str = ""


_state = _State(started=time.monotonic())
_prewarm_task: asyncio.Task | None = None


def note_intent(now: float | None = None) -> None:
    """The user is about to use Wisp (the app opened Ask or the Wisp window)."""
    _state.last_intent = time.monotonic() if now is None else now


def note_parked(reason: str, now: float | None = None) -> None:
    """The chat model was unloaded on Wisp's side (here, or /unload_agent)."""
    _state.parked = True
    _state.parked_at = time.monotonic() if now is None else now
    _state.window_started = None
    _state.last_reason = reason


def _since_turn(now: float) -> float:
    last = idle.last_turn()
    return now - (_state.started if last is None else last)


def _since_intent(now: float) -> float:
    return float("inf") if _state.last_intent is None else now - _state.last_intent


def decide(*, now: float, loaded: bool, busy: bool, pressure: str, enabled: bool,
           last_used: float | None) -> str:
    """What to do with the chat model this tick. Pure apart from reading _state
    and idle.last_turn(), so tests can drive it with a clock."""
    if not enabled:
        return LEAVE
    since_user = min(_since_turn(now), _since_intent(now))
    tight = pressure in (WARN, CRITICAL)
    if loaded:
        if busy:
            return KEEP
        window = _state.window_started
        if window is not None:
            quiet = last_used is None or now - last_used >= BATCH_QUIET_S
            age = now - window
            if tight or age >= BATCH_MAX_S or (age >= BATCH_MIN_S and quiet):
                return CLOSE_WINDOW
            return KEEP
        if tight and since_user >= PRESSURE_GRACE_S:
            return PARK
        if _since_turn(now) >= PARK_AFTER_S and _since_intent(now) >= INTENT_HOLD_S:
            return PARK
        return KEEP
    # Not resident. Only a model Wisp parked gets batch windows; one that was never
    # loaded, or that the user unloaded in oMLX, is left alone.
    if not _state.parked or busy or tight:
        return LEAVE
    last = _state.last_window if _state.last_window is not None else _state.parked_at
    if last is None or now - last >= BATCH_EVERY_S:
        return OPEN_WINDOW
    return LEAVE


async def tick(client, now: float | None = None, *, pressure: str | None = None) -> str:
    """One pass, called from idle_unloader.run every poll."""
    from service.config import role_to_model
    if not getattr(client, "managed", True):
        return LEAVE
    now = time.monotonic() if now is None else now
    model = role_to_model("fast")
    if model not in client.keep_warm():
        return LEAVE
    loaded = model in await client.loaded_models()
    window = _state.window_started
    if loaded and window is not None and min(_since_turn(now), _since_intent(now)) < now - window:
        _state.window_started = None   # the user came back during the window: theirs now
    if loaded and _state.window_started is None:
        _state.parked = False          # a turn, intent or the switch loaded it again
    busy = idle.foreground_busy() or idle.in_flight(model) > 0
    enabled = save_memory()
    if not enabled:
        # Off means resident as before: bring back a model Wisp parked.
        if _state.parked and not loaded and not busy:
            _state.parked = False
            try:
                await client.load(model)
            except Exception as exc:  # noqa: BLE001 — a turn loads it anyway
                log.info("memory saver: reload after switching off failed: %s", type(exc).__name__)
        return LEAVE
    level = pressure_level() if pressure is None else pressure
    action = decide(now=now, loaded=loaded, busy=busy, pressure=level, enabled=enabled,
                    last_used=idle.last_used(model))
    if action in (PARK, CLOSE_WINDOW):
        # Looked again right before the unload: a turn may have started since.
        if idle.foreground_busy() or idle.in_flight(model) > 0:
            return KEEP
        await client.unload(model)
        idle.forget(model)
        note_parked("pressure" if level in (WARN, CRITICAL) else
                    "batch done" if action == CLOSE_WINDOW else "idle", now)
        log.info("memory saver: unloaded %s (%s)", model, _state.last_reason)
    elif action == OPEN_WINDOW:
        _state.last_window = now
        try:
            await client.load(model)
        except Exception as exc:  # noqa: BLE001 — try again at the next window
            log.info("memory saver: batch window load failed: %s", type(exc).__name__)
            return LEAVE
        _state.window_started = now
        log.info("memory saver: batch window open for %s", model)
    return action


async def prewarm(client) -> str:
    """Load the chat model early on intent; returns at once. 'loading', or why not."""
    global _prewarm_task
    from service.config import role_to_model
    from service.inference.remote_policy import remote_only_profile
    note_intent()
    if remote_only_profile() is not None or not getattr(client, "managed", True):
        return "remote"
    if idle.foreground_busy():
        return "busy"                  # the turn loads it
    if pressure_level() == CRITICAL:
        return "pressure"              # leave it to a turn
    if _prewarm_task is not None and not _prewarm_task.done():
        return "loading"
    model = role_to_model("fast")

    async def _load() -> None:
        try:
            if model not in await client.loaded_models():
                await client.load(model)
        except Exception as exc:  # noqa: BLE001 — best effort: the turn loads it anyway
            log.info("memory saver: prewarm failed: %s", type(exc).__name__)

    _prewarm_task = asyncio.create_task(_load())
    return "loading"


def state() -> dict:
    now = time.monotonic()
    return {"save_memory": save_memory(), "parked": _state.parked,
            "batch_window": _state.window_started is not None,
            "pressure": pressure_level(), "minutes_since_turn": round(_since_turn(now) / 60, 1),
            "reason": _state.last_reason}
