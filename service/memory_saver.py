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
- Never unload while a generation runs on the model, a user's turn is in
  flight, or a hold() is open. Parking never refuses a turn: a turn loads the
  model on demand, as it always has ("Loading model… (Ns)").
- Work the user started that loads the model itself between calls (a Research
  job, a Smart Search answer) runs under hold(): its steps leave the model idle
  between calls (web fetches), and parking it there would reload it every step.
- Background model work is resident-only (mail_gists._resident_reply,
  mail_memory's _stand_down, memory capture, the behavior phraser all stand down
  when the model isn't loaded), so parking pauses it with no change to any job.
  Coming back after a park owes the Brief a catch-up (proactive/return_refresh),
  which runs on the model the user's turn or intent loaded, never on a window.
  A batch window loads the model about once an hour so that work catches up,
  then parks it again. Background calls never extend a park timer: only the
  user's turns and intent do.
- Memory pressure parks early: critical after PRESSURE_GRACE_S, warn (routine
  on a 16 GB Mac) after WARN_GRACE_S. Either holds batch windows off.
- Intent (the app opening Ask or the Wisp window, the Ask hotkeys) loads the
  model early so the reload hides behind typing, and holds it INTENT_HOLD_S.
- The "Save memory" switch (on by default) turns parking and batch windows off;
  the chat model then stays resident as before (the notch's X still unloads it,
  as it did before Save memory, but that no longer counts as parked).
- A resident-only background caller checks residency, then awaits before its
  call registers in idle.in_flight; a tick in that gap unloads the model and the
  call reloads it. That costs one load and heals on the next tick; it is not
  closed here.
- On the user's own oMLX.app (engine.mode external-omlx) the app's models may be
  shared with other apps, so Save memory is narrower there:
  - It only ever unloads, or loads, the keep-warm chat model, and only when that
    model is a Gemma that isn't pinned (_is_gemma says how that is decided). Every
    other model is never named in an unload or a load here.
  - It never loads while another model is loaded or loading, because a load can
    make oMLX evict its least recently used model. A turn loads as it always has.
  - oMLX's last_access counts every client's requests, without saying whose. Later
    than Wisp's own last request means another app used the model, and that holds
    the park for the same PARK_AFTER_S, under memory pressure too. Wisp's own
    background calls are its own, so they never hold it.
  - POST /unload aborts running requests, so a park first re-reads oMLX's counters
    for requests in flight and refuses on anything but a clear zero. An app's
    request landing in the milliseconds between that read and the unload can still
    be aborted (it gets a 409 and can retry); oMLX has no atomic unload-if-idle
    over HTTP.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from service import idle
from service.paths import MOE_DIR

log = logging.getLogger(__name__)

# No turn for this long parks the model.
PARK_AFTER_S = 30 * 60
# Intent without a turn keeps the model this long.
INTENT_HOLD_S = 5 * 60
# Under memory pressure, park once the user has been quiet this long: critical
# soon, warn (which a 16 GB Mac reaches in ordinary use) later.
PRESSURE_GRACE_S = 2 * 60
WARN_GRACE_S = 10 * 60
# While parked, a batch window opens at most this often.
BATCH_EVERY_S = 60 * 60
# A window stays open at least BATCH_MIN_S, closes once background calls have
# been quiet for BATCH_QUIET_S, and never stays open past BATCH_MAX_S. The minimum
# outlasts the longest wait a job takes after a "not loaded" stand-down (the
# proactive cycle's IDLE_GAP_S of 300 s, plus the scheduler's 30 s tick), so every
# job gets its turn. The quiet gap outlasts the mail memory walk's wait after
# someone else's call (QUIET_AFTER_USE_S 120 s + BUSY_GAP_S 60 s + a 30 s tick),
# so the walk isn't starved behind the proactive cycle.
BATCH_MIN_S = 6 * 60
BATCH_QUIET_S = 4 * 60
BATCH_MAX_S = 15 * 60
# oMLX stamps last_access when a request's lease ends, a moment after Wisp's call
# returns. A stamp later than Wisp's own by more than this was someone else's.
OWN_ACCESS_SLOP_S = 5.0

SETTINGS_PATH = MOE_DIR / "memory_saver" / "settings.json"
_KEY = "save_memory"

KEEP, PARK, OPEN_WINDOW, CLOSE_WINDOW, LEAVE = "keep", "park", "open_window", "close_window", "leave"

# Why the switch can't act. The app words each one (QuickSettingsText.swift).
NOT_GEMMA, PINNED, NOT_REPORTED, NOT_OMLX = "not_gemma", "pinned", "not_reported", "not_omlx"


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
    last_window: float | None = None     # when the window that last parked it opened
    last_reason: str = ""
    started_wall: float = 0.0            # wall clock at started, for oMLX's last_access
    omlx_model: str | None = None        # the chat model the last look at oMLX.app was about
    omlx_block: str | None = None        # why Save memory can't act on it (None: it can)
    other_app_at: float | None = None    # when another app last used it (oMLX's wall clock)


_state = _State(started=time.monotonic(), started_wall=time.time())
_prewarm_task: asyncio.Task | None = None
_holds = 0


@contextlib.contextmanager
def hold():
    """Keep the chat model resident while user-started work runs (Research, a
    Smart Search answer). A counter: holds overlap."""
    global _holds
    _holds += 1
    try:
        yield
    finally:
        _holds = max(0, _holds - 1)


def held() -> bool:
    return _holds > 0


@contextlib.contextmanager
def unheld():
    """Inside a hold(), let go while the work waits on the user (a paused Research
    job), so a forgotten pause doesn't keep the model resident for good."""
    global _holds
    released = _holds > 0          # outside a hold there is nothing to give back
    if released:
        _holds -= 1
    try:
        yield
    finally:
        if released:
            _holds += 1


def bundled_engine(client=None) -> bool:
    """Wisp serves the chat model from its own bundled engine (and `client` talks
    to it). Save memory acts only there: on the user's oMLX.app another app may be
    using the same model, and Wisp sees only its own calls, so parking it could
    pull the model out from under that app."""
    try:
        from service.engine import BUNDLED_PORT
        from service.engine.mode import is_bundled
        if not is_bundled():
            return False
        if client is None:
            return True
        return (getattr(client, "managed", False) and
                str(getattr(client, "base_url", "")).rstrip("/") == f"http://127.0.0.1:{BUNDLED_PORT}")
    except Exception:  # noqa: BLE001 — unknown engine: leave it alone
        return False


def omlx_app(client=None) -> bool:
    """Wisp serves the chat model from the user's own oMLX app (and `client` is the
    local client that talks to it). Not a remote profile, not another local server."""
    try:
        from service.config.endpoints import is_loopback, role_target
        from service.engine.mode import is_bundled
        from service.inference.remote_policy import remote_only_profile
        if is_bundled() or remote_only_profile() is not None:
            return False
        if client is None:
            endpoint = role_target("fast").endpoint
            return endpoint.managed and endpoint.provider == "omlx"
        return (getattr(client, "managed", False) and
                getattr(getattr(client, "provider", None), "omlx_health", False) and
                is_loopback(str(getattr(client, "base_url", ""))))
    except Exception:  # noqa: BLE001 — unknown engine: leave it alone
        return False


def _is_gemma(row: dict) -> bool:
    """oMLX's row is a Gemma: its config.json says so AND its id does. The config
    alone isn't enough, since NVIDIA's Nemotron Bolt is built on gemma4; the id alone
    is only a name. A tuned Gemma (gemma-4-E4B-it-r4-step-…) passes both, with no
    list of ids to keep current. The Gemma assistant draft models are helpers."""
    kind, name = row.get("config_model_type"), row.get("id")
    return (isinstance(kind, str) and isinstance(name, str) and kind.startswith("gemma")
            and "assistant" not in kind and "gemma" in name.lower()
            and not row.get("is_helper"))


@dataclass
class _OmlxView:
    row: dict = field(default_factory=dict)
    others: list[str] = field(default_factory=list)   # other models loaded or loading
    block: str | None = None


async def _look_at_omlx(client, model: str) -> _OmlxView:
    """One read of oMLX's model list, and what it means for Save memory."""
    rows = [r for r in (await client.status()).get("models", []) if isinstance(r, dict)]
    row = next((r for r in rows if r.get("id") == model), None)
    others = [r["id"] for r in rows
              if r.get("id") != model and (r.get("loaded") or r.get("is_loading"))]
    if row is None:
        return _OmlxView(others=others, block=NOT_REPORTED)
    block = None
    if any(key not in row for key in ("config_model_type", "last_access", "pinned")):
        block = NOT_REPORTED           # no way to tell what it is or who used it: never unload blind
    elif not _is_gemma(row):
        block = NOT_GEMMA
    elif row.get("pinned"):
        block = PINNED
    return _OmlxView(row=row, others=others, block=block)


def _stamp(row: dict) -> float | None:
    """The row's last_access, when it is a real time."""
    access = row.get("last_access")
    if isinstance(access, bool) or not isinstance(access, (int, float)) or access <= 0:
        return None
    return float(access)


def _other_app_recent(model: str, row: dict, wall: float) -> bool:
    """Another app used the model within PARK_AFTER_S.

    Run every tick, so a use is remembered even if one of Wisp's own later replaces
    it in last_access. A use and a later one of Wisp's inside a single tick can't be
    told apart; an app that keeps using the model shows up on other ticks."""
    access = _stamp(row)
    if access is not None and not idle.own_access_active(model):
        # A stamp from before this backend started says nothing about who made it;
        # the park timer already counts from the start.
        ours = max(idle.own_access(model) or 0.0, _state.started_wall)
        if access > ours + OWN_ACCESS_SLOP_S:
            _state.other_app_at = max(_state.other_app_at or 0.0, access)
    at = _state.other_app_at
    return at is not None and wall - at < PARK_AFTER_S


async def _quiet_on_omlx(client, model: str, seen: dict) -> bool:
    """Right before an unload: nothing is running on oMLX and nothing touched the
    model since the tick's read. POST /unload aborts running requests, whoever's they
    are, and last_access stops moving during one long generation. Anything unclear
    is a no: the next tick asks again."""
    try:
        counts = await client.engine_activity()
        fresh = await _look_at_omlx(client, model)
    except Exception as exc:  # noqa: BLE001 — unknown means don't unload
        log.info("memory saver: oMLX activity unreadable: %s", type(exc).__name__)
        return False
    for key in ("active_requests", "waiting_requests"):
        value = counts.get(key)
        if type(value) is not int or value != 0:
            return False
    return (fresh.block is None and fresh.row.get("loaded") is True
            and _stamp(fresh.row) is not None
            and _stamp(fresh.row) == _stamp(seen)
            # The reads above awaited: a turn or a hold may have started meanwhile.
            and not (idle.foreground_busy() or idle.in_flight(model) > 0 or held()))


def _acts_on_omlx() -> bool:
    """The last look at oMLX.app found a chat model Save memory may act on."""
    try:
        from service.config import role_to_model
        return (omlx_app() and _state.omlx_block is None
                and _state.omlx_model == role_to_model("fast"))
    except Exception:  # noqa: BLE001 — unknown: leave it alone
        return False


def note_notch_hidden(now: float | None = None) -> None:
    """/unload_agent (the notch's X) unloaded the chat model. Parked only with the
    switch on, where Save memory acts: otherwise the old behavior stands (unloaded
    until next use) rather than a reload 30 s later."""
    if save_memory() and (bundled_engine() or _acts_on_omlx()):
        note_parked("notch hidden", now)


def note_intent(now: float | None = None) -> None:
    """The user is about to use Wisp (the app opened Ask or the Wisp window)."""
    _state.last_intent = time.monotonic() if now is None else now


def note_parked(reason: str, now: float | None = None, *, window_closed: bool = False) -> None:
    """The chat model was unloaded on Wisp's side (here, or /unload_agent).

    The next window is an hour after this park, unless the park closed a window
    (for any reason, pressure included): then an hour after that window opened.
    Without the reset, a window from the morning would reload the model on the
    tick after an evening park."""
    _state.parked = True
    _state.parked_at = time.monotonic() if now is None else now
    if not window_closed:
        _state.last_window = None
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
    # Warn keeps windows shut too. On a 16 GB Mac warn is routine, so there the
    # resident-only background work often waits for the next use of Wisp.
    tight = pressure in (WARN, CRITICAL)
    grace = PRESSURE_GRACE_S if pressure == CRITICAL else WARN_GRACE_S
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
        if tight and since_user >= grace:
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


async def tick(client, now: float | None = None, *, pressure: str | None = None,
               wall: float | None = None) -> str:
    """One pass, called from idle_unloader.run every poll."""
    from service.config import role_to_model
    bundled = bundled_engine(client)
    if not bundled and not omlx_app(client):
        return LEAVE
    now = time.monotonic() if now is None else now
    model = role_to_model("fast")
    if model not in client.keep_warm():
        return LEAVE
    view = None
    if bundled:
        loaded = model in await client.loaded_models()
    else:
        view = await _look_at_omlx(client, model)
        _state.omlx_model, _state.omlx_block = model, view.block
        if view.block is not None:     # not one Save memory may touch
            _state.parked = False
            _state.window_started = None
            return LEAVE
        if view.row.get("is_loading"):
            return LEAVE
        loaded = view.row.get("loaded") is True
    window = _state.window_started
    if loaded and window is not None and min(_since_turn(now), _since_intent(now)) < now - window:
        _state.window_started = None   # the user came back during the window: theirs now
    if loaded and _state.window_started is None:
        if _state.parked and (view is None or _user_since_park(now)):
            _note_unparked()           # someone is back: catch the Brief up if it went stale
        _state.parked = False          # a turn, intent or the switch loaded it again
    busy = idle.foreground_busy() or idle.in_flight(model) > 0 or held()
    if view is not None:
        # Counts as busy, so it holds a park, a window closing and a window opening.
        wall = time.time() if wall is None else wall
        busy = _other_app_recent(model, view.row, wall) or busy
    enabled = save_memory()
    if not enabled:
        # Off means resident as before: bring back a model Wisp parked.
        if _state.parked and not loaded and not busy:
            _state.parked = False
            if view is not None and view.others:
                return LEAVE           # a load could evict them: leave it to a turn
            try:
                await _load_shared(client, model)
            except Exception as exc:  # noqa: BLE001 — a turn loads it anyway
                log.info("memory saver: reload after switching off failed: %s", type(exc).__name__)
        return LEAVE
    level = pressure_level() if pressure is None else pressure
    action = decide(now=now, loaded=loaded, busy=busy, pressure=level, enabled=enabled,
                    last_used=idle.last_used(model))
    if action in (PARK, CLOSE_WINDOW):
        if view is not None and not await _quiet_on_omlx(client, model, view.row):
            return KEEP
        await client.unload(model)
        idle.forget(model)
        note_parked("pressure" if level in (WARN, CRITICAL) else
                    "batch done" if action == CLOSE_WINDOW else "idle", now,
                    window_closed=action == CLOSE_WINDOW)
        log.info("memory saver: unloaded %s (%s)", model, _state.last_reason)
    elif action == OPEN_WINDOW:
        if view is not None and view.others:
            return LEAVE               # a load could evict them: try again next tick
        _state.last_window = now
        try:
            await _load_shared(client, model)
        except Exception as exc:  # noqa: BLE001 — try again at the next window
            log.info("memory saver: batch window load failed: %s", type(exc).__name__)
            return LEAVE
        _state.window_started = now
        log.info("memory saver: batch window open for %s", model)
    return action


def _user_since_park(now: float) -> bool:
    """A turn or intent came after the park. On oMLX.app another app can load the
    model too, and that isn't the user coming back to Wisp."""
    parked_at = _state.parked_at
    return parked_at is None or min(_since_turn(now), _since_intent(now)) < now - parked_at


def _note_unparked() -> None:
    try:
        from service.proactive import return_refresh
        return_refresh.arm("unparked")
    except Exception:  # noqa: BLE001 — the hourly idle refresh still catches up
        pass


def batch_window_open() -> bool:
    """A batch window loaded the model for background work (nobody is here)."""
    return _state.window_started is not None


async def _load_shared(client, model: str) -> None:
    """Load through the slot settle_prewarm waits on, so a turn that starts while
    a window (or the switch turning off) loads the model waits for that load
    instead of POSTing a second one. A prewarm already loading it is reused."""
    global _prewarm_task
    task = _prewarm_task
    if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
        task = _prewarm_task = asyncio.create_task(client.load(model))
    await task


async def prewarm(client) -> str:
    """Load the chat model early on intent; returns at once. 'loading', or why not."""
    global _prewarm_task
    from service.config import role_to_model
    from service.inference.remote_policy import remote_only_profile
    note_intent()
    if remote_only_profile() is not None or not getattr(client, "managed", True):
        return "remote"
    bundled = bundled_engine(client)
    if not bundled and not (omlx_app(client) and _state.parked):
        return "external"              # only a model Save memory parked comes back early there
    if idle.foreground_busy():
        return "busy"                  # the turn loads it
    if pressure_level() == CRITICAL:
        return "pressure"              # leave it to a turn
    if _prewarm_task is not None and not _prewarm_task.done():
        return "loading"
    model = role_to_model("fast")

    async def _load() -> None:
        try:
            if not bundled:
                view = await _look_at_omlx(client, model)
                if view.block is not None or view.others or view.row.get("is_loading"):
                    return             # a load could evict them: leave it to a turn
            if model not in await client.loaded_models():
                await client.load(model)
        except Exception as exc:  # noqa: BLE001 — best effort: the turn loads it anyway
            log.info("memory saver: prewarm failed: %s", type(exc).__name__)

    _prewarm_task = asyncio.create_task(_load())
    return "loading"


def prewarm_pending() -> bool:
    """A prewarm load is in flight on this loop (what settle_prewarm would wait on)."""
    task = _prewarm_task
    if task is None or task.done():
        return False
    try:
        return task.get_loop() is asyncio.get_running_loop()
    except RuntimeError:
        return False


async def settle_prewarm(timeout: float = 15.0) -> None:
    """Wait for a prewarm load in flight, so a turn's ensure_only doesn't POST a
    second load of the same model beside it (or evict others mid-load). Only on
    the prewarm's own loop; the behavior phraser runs a loop of its own."""
    task = _prewarm_task
    if task is None or task.done():
        return
    try:
        if task.get_loop() is not asyncio.get_running_loop():
            return
        await asyncio.wait({task}, timeout=timeout)
    except Exception:  # noqa: BLE001 — never hold a turn up on this
        return


def _scope() -> tuple[bool, str | None]:
    """Whether Save memory can act right now, and if not, why not."""
    if bundled_engine():
        return True, None
    if not omlx_app():
        return False, NOT_OMLX
    try:
        from service.config import role_to_model
        model = role_to_model("fast")
    except Exception:  # noqa: BLE001 — no chat model configured
        return False, NOT_REPORTED
    if _state.omlx_model == model:
        return _state.omlx_block is None, _state.omlx_block
    # No look at oMLX for this model yet (the next tick is at most 30 s away).
    return (True, None) if "gemma" in model.lower() else (False, NOT_GEMMA)


def state() -> dict:
    now = time.monotonic()
    applies, why_not = _scope()
    at = _state.other_app_at
    return {"save_memory": save_memory(), "applies": applies, "why_not": why_not,
            "engine": "bundled" if bundled_engine() else "omlx" if omlx_app() else "remote",
            "parked": _state.parked,
            "other_app": applies and at is not None and time.time() - at < PARK_AFTER_S,
            "batch_window": _state.window_started is not None,
            "pressure": pressure_level(), "held": held(),
            "minutes_since_turn": round(_since_turn(now) / 60, 1),
            "reason": _state.last_reason}
