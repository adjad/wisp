"""GPU power caps on Wisp's own model calls: on battery, and while the user is away.

oMLX (0.7.3+) takes a per-request ``gpu_power_limit_w`` and holds a firmware
GPU power cap while that request generates. The cap is a limit on the whole
Mac's GPU, not just Wisp's, and oMLX holds the LOWEST cap among the capped
requests in flight; an uncapped request does not register with it at all, so it
runs under whatever cap another request holds. So Wisp only ever asks per
request, never as a model-level setting (which would cap every request).

Measured on the M5 Pro with the Gemma E4B roster (2026-10-27, real capped
requests; the admin benchmark never applies the cap):

    cap    first token   decode   GPU J/token
    none   1.16 s        84 t/s   0.270
    15 W   +25%          -5%      -36%
    10 W   +35%          -16%     -52%

IMPORTANT rules:
- A cap must not slow another app. macOS has no per-app GPU cap, so Wisp caps
  a call only while other apps use under OTHER_GPU_BUSY of the GPU, looks
  again right before every capped request, keeps looking every WATCH_EVERY_S
  while one runs, and lifts the cap when another app starts (see "other apps
  on the GPU" below). An app that starts mid-request can still be slowed until
  the next look and the lift, about a second. Any doubt (a failed or slow
  read, no recent reading) means uncapped.
- Only Wisp's bundled engine is ever capped. It drops the cap the moment the
  last capped request ends (``GpuPowerLimiter(release_delay_s=0.0)``, since
  0.5.0-dev6) and whenever Wisp asks, even mid-request (``POST
  /v1/wisp/gpu-power/lift``, build-support/engine_guard, since 0.5.0-dev7). The
  user's own oMLX.app holds a cap 15 s past its request and can't lift one, so
  it never gets a cap.
- A call someone is waiting on never runs under a lower cap than its own: one
  starting while a lower-capped call is in flight lifts that cap, and no chat
  call is capped while a call someone waits on runs uncapped (research, voice
  transcription).
- Only calls on the service's own event loop are capped (see "calls in flight").

With the "Save power" switch on (the notch; on by default; stored as
``save_when_away``):

- ``CHAT`` (chat replies: Ask turns typed, spoken or from the phone, with the
  history folding at the end of a turn, and questions about an attached image
  or document): 15 W while the Mac is on battery and the turn is still
  running; uncapped on AC. "On battery" is pmset's "Battery Power"; a desktop
  drawing from a UPS reports "UPS Power" and counts as battery too.
- ``BRIEF`` (Brief generation: the mail and texts gists, the inbox summary, the
  proactive triage/judge/prepare behind its cards): 15 W on battery, and 10 W
  while the user is away; uncapped otherwise.
- ``BACKGROUND`` (the mail memory walk, the behavior tick's interpreter,
  memory capture): 10 W always, plugged in too (the user's answer, 2026-10-28:
  "Always 10 W"). The behavior phraser is tiered BACKGROUND too but runs on its
  own event loop, so it is never capped (see "calls in flight").
- "Away" is no keyboard or mouse input for AWAY_AFTER_S and no turn running.
- Everything else is never capped: research, connection investigations the
  user asks for (memory/connections.py), voice transcription, setup probes.

The Daily Summary, scheduled or opened, makes no model call
(service/assistant/brief.py renders it from caches), so there is nothing to cap.

While any non-background call is in flight (chat, research, ...), BRIEF and
BACKGROUND calls go uncapped, so they can't drag it below its own cap.

A tier is set with ``with power_tier(CHAT):`` around the call (it is a
ContextVar, so tasks created inside inherit it). OMLXClient adds the field only
for the managed local engine, never for a remote provider.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from service.paths import MOE_DIR

logger = logging.getLogger(__name__)

CHAT = "chat"
BRIEF = "brief"
BACKGROUND = "background"
_TIERS = (CHAT, BRIEF, BACKGROUND)
_UNWATCHED = (BRIEF, BACKGROUND)          # nobody is waiting on these

# The two caps: LOW for background work (and the Brief while the user is away),
# BATTERY for chat replies and the Brief on battery.
LOW = "low"
BATTERY = "battery"
_DEFAULT_WATTS = {LOW: 10, BATTERY: 15}
# 0 turns the cap off; anything outside oMLX's 2-1000 W range is ignored.
_ENV = {LOW: "WISP_BACKGROUND_GPU_W", BATTERY: "WISP_BATTERY_GPU_W"}
_MIN_W, _MAX_W = 2, 1000

SETTINGS_PATH = MOE_DIR / "power" / "settings.json"
_KEY = "save_when_away"

_tier: ContextVar[str | None] = ContextVar("wisp_power_tier", default=None)


# --- the switch -------------------------------------------------------------------

def save_when_away(path: Path | None = None) -> bool:
    """The "Save power" switch (named for its first rule). On unless the user
    turned it off; off means nothing is ever capped."""
    path = SETTINGS_PATH if path is None else path
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or unreadable means the default
        return True
    return not (isinstance(data, dict) and data.get(_KEY) is False)


def set_save_when_away(on: bool, path: Path | None = None) -> bool:
    if type(on) is not bool:
        raise ValueError("save_when_away must be true or false")
    path = SETTINGS_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({_KEY: on}, f)
    tmp.replace(path)
    return on


# --- away and on battery ----------------------------------------------------------
#
# Away is read from the keyboard/mouse idle time (IOHIDSystem), and the power
# source from pmset (the mail memory walk's cached probes). Both run a
# subprocess, so they are never read on a request: the scheduler refreshes them
# off the event loop every tick, and a reading older than _AWAY_STALE_S counts
# as present and on AC (uncapped).
#
# Age is checked on both clocks. time.monotonic() stops while the Mac sleeps, so
# a reading taken just before sleep would look fresh at wake and cap the wake
# backfill while the user is back; the wall clock counts the sleep. A wall
# clock set back (a reading "from the future") also counts as stale.

AWAY_AFTER_S = 600.0
_AWAY_STALE_S = 120.0
_away: tuple[float, float, bool] | None = None      # (monotonic, wall time read, away)
_battery: tuple[float, float, bool] | None = None   # (monotonic, wall time read, on battery)


def _remote_only() -> bool:
    try:
        from service.inference.remote_policy import remote_only_profile
        return remote_only_profile() is not None
    except Exception:  # noqa: BLE001 — unreadable policy: assume a local engine
        return False


def _bundled_engine() -> bool:
    """Wisp serves its models from its bundled engine, not the user's oMLX.app."""
    try:
        from service.engine.mode import is_bundled
        return is_bundled()
    except Exception:  # noqa: BLE001 — unknown engine: never cap it
        return False


def _capping_possible() -> bool:
    """The switch is on and the engine can drop a cap at once (see the module rules)."""
    return save_when_away() and _lift_askable() and not _remote_only() and _bundled_engine()


def _on_battery_probe() -> bool:
    from service.mail_memory.runner import _cached, _power
    on_ac, _percent, _low_power = _cached("power", _power)   # a failed read says AC
    return not on_ac


def refresh_away(hid_idle_s: float | None = None, on_battery: bool | None = None, *,
                 probe: bool = True) -> bool:
    """Read the input idle time and the power source, and store whether the
    user is away and whether the Mac is on battery. Also keep a GPU baseline
    (background work can be capped at any time), so the look before each
    capped request compares against a reading at most a tick old. Blocking:
    call it from a worker thread. Returns away.

    With the switch off, or no engine Wisp may cap (a remote-only profile, the
    user's own oMLX.app), nothing could be capped, so nothing is read: no ioreg,
    no pmset."""
    global _away, _battery
    if not _capping_possible():
        drop_gpu_readings()
        mono, wall = time.monotonic(), time.time()
        _away = _battery = (mono, wall, False)
        return False
    if probe and hid_idle_s is None:
        from service.mail_memory.runner import _hid_idle_s
        hid_idle_s = _hid_idle_s()
    if probe and on_battery is None:
        on_battery = _on_battery_probe()
    idle_enough = hid_idle_s is not None and hid_idle_s >= AWAY_AFTER_S
    battery = on_battery is True
    keep_gpu_baseline()
    mono, wall = time.monotonic(), time.time()
    _away = (mono, wall, idle_enough)
    _battery = (mono, wall, battery)
    return idle_enough


def _fresh(reading: tuple[float, float, bool] | None, max_age_s: float = _AWAY_STALE_S) -> bool:
    if reading is None:
        return False
    mono, wall, value = reading
    return value and time.monotonic() - mono < max_age_s and 0 <= time.time() - wall < max_age_s


# --- other apps on the GPU --------------------------------------------------------
#
# The cap limits the whole Mac's GPU, so anything else using it at the same time
# (a game, a video, an export, a render, another app's model) would be throttled
# too. A look reads every process's accumulated GPU time (ioreg
# AGXDeviceUserClient AppUsage, readable without privileges, ~20 ms) and
# compares it with the previous read: if other apps used at least
# OTHER_GPU_BUSY of the GPU in between, nothing is capped. A share averaged over
# a long window would hide an app that started a moment ago, so when the
# previous read is older than _GPU_WINDOW_S the look also reads twice
# _GPU_PAIR_GAP_S apart, and both windows must be quiet. That pair costs the
# first capped call after a pause (the first call of a chat turn on battery)
# about 0.27-0.3 s; later calls compare with the watch's samples, one read.
#
# Wisp looks right before each capped request (bounded by _LOOK_TIMEOUT_S; a
# slow look means uncapped) and every WATCH_EVERY_S while one runs; the
# scheduler tick only keeps a baseline at most a tick old while a cap could
# apply. Wisp itself (the app and its bundled engine) and WindowServer's
# compositing don't count; the user's oMLX.app does, since it is never Wisp's
# capped engine. No baseline, a baseline older than _GPU_BASELINE_MAX_S, a
# failed read (then no read for _GPU_RETRY_S), or a look already running
# counts as busy (uncapped).

# OTHER_GPU_BUSY is the user's choice (2026-10-28: "increase the gpu cap to
# 7.5%"; it was 2%). An app redrawing its window reads 5-7.3% of an idle GPU,
# so at 2% a cap almost never applied while the user was at the Mac. Measured
# the same day: a steady 120 Hz render load reading 7.7% of an idle GPU, beside
# a Gemma reply capped at 15 W or 10 W, finished each frame within 0.1 ms of
# the uncapped run and missed no frame (the GPU runs faster beside a reply
# than idle, so the same load reads only 1-3% there).
OTHER_GPU_BUSY = 0.075
WATCH_EVERY_S = 1.0
_GPU_MIN_GAP_S = 0.5           # a shorter window can't tell: reuse the last answer
_GPU_WINDOW_S = 1.5            # an older baseline needs a short second window too
_GPU_PAIR_GAP_S = 0.25
_GPU_BASELINE_MAX_S = 120.0
_GPU_FRESH_S = 2.0             # a cap needs an answer at most this old
_GPU_RETRY_S = 10.0
_GPU_LOCK_WAIT_S = 0.5
_LOOK_TIMEOUT_S = 0.75
_OWN_PATH_PARTS = ("/Wisp.app/",)
_gpu_lock = threading.Lock()
_gpu_sample: tuple[float, dict[int, int]] | None = None    # (monotonic, {pid: GPU ns})
_gpu_answer: tuple[float, float, bool] | None = None       # (monotonic, wall, quiet)
_gpu_failed_at: float | None = None
_BLOCK = re.compile(r"\+-o AGXDeviceUserClient")
_CREATOR = re.compile(r'"IOUserClientCreator" = "pid (\d+),')
_GPU_NS = re.compile(r'"accumulatedGPUTime"=(\d+)')


def _parse_gpu_times(raw: str) -> dict[int, int]:
    """{pid: total accumulated GPU ns} from `ioreg -r -c AGXDeviceUserClient -l`."""
    totals: dict[int, int] = {}
    for block in _BLOCK.split(raw)[1:]:
        creator = _CREATOR.search(block)
        if creator:
            pid = int(creator.group(1))
            totals[pid] = totals.get(pid, 0) + sum(int(ns) for ns in _GPU_NS.findall(block))
    return totals


@functools.lru_cache(maxsize=1)
def _engine_root() -> str | None:
    """The bundled engine's code root; fixed for the process (WISP_ENGINE_ROOT or beside the service)."""
    try:
        from service.engine.mode import engine_code_root
        return str(engine_code_root()) + "/"
    except Exception:  # noqa: BLE001
        return None


def _own(pid: int) -> bool:
    """Wisp, its bundled engine, or WindowServer. An unreadable path counts as another app."""
    from service.inference.local_peer import process_path
    try:
        path = process_path(pid)
    except Exception:  # noqa: BLE001 — a process that exited or can't be read
        return False
    root = _engine_root()
    return (any(part in path for part in _OWN_PATH_PARTS) or path.endswith("/WindowServer")
            or (root is not None and path.startswith(root)))


def _other_gpu_times() -> dict[int, int] | None:
    try:
        raw = subprocess.run(["/usr/sbin/ioreg", "-r", "-c", "AGXDeviceUserClient", "-l", "-w0"],
                             capture_output=True, text=True, errors="replace", timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return {pid: ns for pid, ns in _parse_gpu_times(raw).items() if not _own(pid)}


def _read() -> tuple[float, dict[int, int]] | None:
    """One sample, or None (and no read for _GPU_RETRY_S) when ioreg fails. Holds _gpu_lock."""
    global _gpu_failed_at
    if _gpu_failed_at is not None and time.monotonic() - _gpu_failed_at < _GPU_RETRY_S:
        return None
    times = _other_gpu_times()
    if times is None:
        _gpu_failed_at = time.monotonic()
        return None
    _gpu_failed_at = None
    return time.monotonic(), times


def _quiet_between(before: tuple[float, dict[int, int]], after: tuple[float, dict[int, int]]) -> bool:
    (then, old), (now, new) = before, after
    if not 0 < now - then <= _GPU_BASELINE_MAX_S:
        return False
    used_ns = sum(max(0, ns - old.get(pid, 0)) for pid, ns in new.items())
    return used_ns / ((now - then) * 1e9) < OTHER_GPU_BUSY


def check_other_gpu() -> bool:
    """Look at other apps' GPU use now and store whether they left the GPU quiet.
    Blocking (ioreg, and a _GPU_PAIR_GAP_S pause after an old baseline): call it
    from a worker thread. Returns quiet."""
    global _gpu_sample, _gpu_answer
    if not _gpu_lock.acquire(timeout=_GPU_LOCK_WAIT_S):
        return False                         # another look is stuck: uncapped
    try:
        previous = _gpu_sample
        if (previous is not None and _gpu_answer is not None
                and time.monotonic() - previous[0] < _GPU_MIN_GAP_S):
            return _gpu_answer[2]
        latest = _read()
        quiet = latest is not None and previous is not None and _quiet_between(previous, latest)
        if quiet and latest[0] - previous[0] > _GPU_WINDOW_S:
            time.sleep(_GPU_PAIR_GAP_S)
            first, latest = latest, _read()
            quiet = latest is not None and _quiet_between(first, latest)
        _gpu_sample = latest
        _gpu_answer = (time.monotonic(), time.time(), quiet)
        return quiet
    finally:
        _gpu_lock.release()


def keep_gpu_baseline() -> None:
    """The scheduler tick: a sample for the next look to compare with. Blocking."""
    global _gpu_sample
    if not _gpu_lock.acquire(timeout=_GPU_LOCK_WAIT_S):
        return
    try:
        _gpu_sample = _read()
    finally:
        _gpu_lock.release()


async def _look() -> bool:
    """check_other_gpu off the event loop, within _LOOK_TIMEOUT_S; slow or failed is busy."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(check_other_gpu), _LOOK_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — a timeout too (the thread finishes on its own)
        return False


def drop_gpu_readings() -> None:
    global _gpu_sample, _gpu_answer
    if not _gpu_lock.acquire(timeout=_GPU_LOCK_WAIT_S):
        return                               # the next tick drops them
    try:
        _gpu_sample = _gpu_answer = None
    finally:
        _gpu_lock.release()


def gpu_quiet() -> bool:
    """Whether the last look, at most _GPU_FRESH_S ago, found the GPU free of other apps."""
    return _fresh(_gpu_answer, _GPU_FRESH_S)


def away() -> bool:
    """Whether the user is away: idle input, a fresh reading, no turn running."""
    from service import idle
    return not idle.foreground_busy() and _fresh(_away)


def on_battery() -> bool:
    """Whether the Mac is on battery, by a fresh reading."""
    return _fresh(_battery)


# --- calls in flight, and lifting a cap ----------------------------------------------
#
# The engine lifts every cap at once on ``POST /v1/wisp/gpu-power/lift``; the
# requests still running finish uncapped. Wisp asks when the watch finds
# another app on the GPU, and when a call someone is waiting on starts beside a
# lower-capped one. Each capped call registers its own client's lift, so the
# lift always goes through a client still in use. The first capped request of
# a run asks once beforehand: an engine that answers anything but 200 (an older
# bundled engine without the route) is never capped that run, and one that
# can't be reached isn't capped for _LIFT_RETRY_S. The watch keeps to that too:
# after a refused lift it doesn't ask again that run, and after an unreachable
# one not before _LIFT_RETRY_S, so a capped request beside a busy GPU doesn't
# POST and warn every second. A call someone waits on that starts beside a
# lower cap still asks (once per call: it's the only way to keep it from
# running under that cap). A lift tries at most _LIFT_TRIES clients, and none
# after one that timed out.
#
# Only calls on the service's own event loop are capped: uvicorn runs it on the
# main thread. The behavior phraser runs asyncio.run in a worker thread with a
# client that closes when it ends, so its calls go uncapped and hold no watch.
# The counters are shared with such threads, so they sit behind _calls_lock.

_LIFT_RETRY_S = 60.0
_LIFT_TIMEOUT_S = 2.0
_LIFT_TRIES = 2                           # a client that just closed fails at once; one more is asked
_calls_lock = threading.Lock()
_watched_calls = 0
_uncapped_watched = 0                     # calls someone waits on that run uncapped
_capped: dict[int, tuple[int, Callable[[], Awaitable[bool | None]]]] = {}   # id: (watts, lift)
_next_call = 0
_lift_ok: bool | None = None              # None: not asked yet (or ask again after _lift_retry_at)
_lift_retry_at = 0.0
_lifted = False                           # the watch lifted the caps in flight already
_service_loop: asyncio.AbstractEventLoop | None = None
_watch_task: asyncio.Task | None = None
_lift_tasks: set[asyncio.Task] = set()


def _on_service_loop() -> bool:
    return threading.current_thread() is threading.main_thread()


async def _ask_lift(lift: Callable[[], Awaitable[bool | None]]) -> bool | None:
    """True: the engine lifted the cap. False: it refused (anything but 200).
    None: it couldn't be asked (not reachable)."""
    try:
        return await lift()
    except Exception:  # noqa: BLE001 — treated as unreachable
        return None


def _lift_askable() -> bool:
    """Not refused this run, and not unreachable within the last _LIFT_RETRY_S."""
    return _lift_ok is not False and time.monotonic() >= _lift_retry_at


def _note_lift(answer: bool | None) -> None:
    """After a lift: a refusal stops capping for the run, unreachable for _LIFT_RETRY_S."""
    global _lift_ok, _lift_retry_at
    if answer is True:
        _lift_ok = True
        return
    if answer is False:
        _lift_ok = False
        logger.warning("The engine refused to lift Wisp's GPU power cap; no more caps this run")
    else:
        _lift_ok = None
        _lift_retry_at = time.monotonic() + _LIFT_RETRY_S
        logger.warning("The engine could not be asked to lift Wisp's GPU power cap; no caps for a minute")


async def _lift_now(*, watch: bool = False) -> None:
    """Lift every cap through a capped call's client, newest first: one that
    closed meanwhile (its call just ended) can't be asked, so the next is, up
    to _LIFT_TRIES; after a timeout none is (the engine is the same). Cheap and
    idempotent on the engine; only the watch marks the caps in flight lifted,
    and only once the engine said it lifted them."""
    global _lifted
    with _calls_lock:
        lifts = [lift for _, lift in reversed(_capped.values())][:_LIFT_TRIES]
    if not lifts:
        return
    answer = None
    give_up_at = time.monotonic() + _LIFT_TIMEOUT_S
    for lift in lifts:
        answer = await _ask_lift(lift)
        if answer is not None or time.monotonic() >= give_up_at:
            break
    if watch and answer is True:
        _lifted = True                       # otherwise the next look asks again
    _note_lift(answer)


def _schedule_lift() -> None:
    """On the service loop (call_soon_threadsafe from another thread)."""
    task = asyncio.get_running_loop().create_task(_lift_now())
    _lift_tasks.add(task)
    task.add_done_callback(_lift_tasks.discard)


async def _watch() -> None:
    """While capped calls run, look at other apps' GPU use every WATCH_EVERY_S.
    A capped call starts only after a quiet look, and re-arms the watch. No
    look while a lift couldn't be asked anyway (see _lift_askable)."""
    global _watch_task
    try:
        while _capped:
            await asyncio.sleep(WATCH_EVERY_S)
            if _capped and not _lifted and _lift_askable() and not await _look():
                await _lift_now(watch=True)
    finally:
        _watch_task = None


def call_started(watts: int | None = None,
                 lift: Callable[[], Awaitable[bool | None]] | None = None) -> tuple[bool, int | None]:
    """OMLXClient, as a managed request goes out with ``watts`` as its cap (None
    for none) and ``lift`` to lift it. Pass the result to call_ended. Counts
    calls someone is waiting on (anything but BRIEF and BACKGROUND) and capped
    calls, watches while capped ones run, and lifts a lower cap a watched call
    would otherwise run under."""
    global _watched_calls, _uncapped_watched, _next_call, _lifted, _watch_task, _service_loop
    watched = current_tier() not in _UNWATCHED
    on_loop = _on_service_loop()
    if watts is not None and (lift is None or not on_loop):
        watts = None                         # never capped off the service loop
    key = None
    with _calls_lock:
        if watched:
            _watched_calls += 1
            if watts is None:
                _uncapped_watched += 1
            lower = bool(_capped) and min(w for w, _ in _capped.values()) < (watts or _MAX_W + 1)
        else:
            lower = False
        if watts is not None:
            key, _next_call = _next_call, _next_call + 1
            _capped[key] = (watts, lift)
    if lower:
        if on_loop:
            _schedule_lift()
        elif _service_loop is not None:
            _service_loop.call_soon_threadsafe(_schedule_lift)
    if watts is not None:
        _lifted = False                      # a new cap is set
        _service_loop = asyncio.get_running_loop()
        if _watch_task is None:
            _watch_task = _service_loop.create_task(_watch())
    return watched, key


def call_ended(started: tuple[bool, int | None]) -> None:
    global _watched_calls, _uncapped_watched
    watched, key = started
    with _calls_lock:
        if watched:
            _watched_calls = max(0, _watched_calls - 1)
            if key is None:
                _uncapped_watched = max(0, _uncapped_watched - 1)
        if key is not None:
            _capped.pop(key, None)


def watched_running() -> bool:
    return _watched_calls > 0


# --- tiers ------------------------------------------------------------------------

@contextmanager
def power_tier(tier: str) -> Iterator[None]:
    """Run the enclosed model calls under ``tier``'s GPU power cap."""
    if tier not in _TIERS:
        raise ValueError(f"unknown power tier: {tier!r}")
    token = _tier.set(tier)
    try:
        yield
    finally:
        _tier.reset(token)


def current_tier() -> str | None:
    return _tier.get()


def watts(cap: str | None) -> int | None:
    """The LOW or BATTERY cap in watts, or None for no cap."""
    if cap not in _DEFAULT_WATTS:
        return None
    raw = os.environ.get(_ENV[cap], "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = _DEFAULT_WATTS[cap]
        if value == 0:
            return None
        if _MIN_W <= value <= _MAX_W:
            return value
    return _DEFAULT_WATTS[cap]


def _wanted(tier: str | None) -> str | None:
    """The cap a call at ``tier`` gets right now if other apps are quiet on the GPU."""
    if tier not in _TIERS or not _on_service_loop() or not _capping_possible():
        return None
    if tier in _UNWATCHED and watched_running():
        return None
    if tier == CHAT and _uncapped_watched:
        return None                          # research, voice, ... would run under it
    if tier in _UNWATCHED and away():
        return LOW
    if tier == CHAT and not _turn_running():
        return None
    if tier == BACKGROUND:
        return LOW
    if tier in (CHAT, BRIEF) and on_battery():
        return BATTERY
    return None


def _turn_running() -> bool:
    """A chat turn (or an image or document question) is in flight. CHAT is a
    ContextVar, so a task spawned inside a turn inherits it; without a turn
    still running, that task's calls are not chat and go uncapped."""
    from service import idle
    return idle.foreground_busy()


def payload_fields() -> dict[str, int]:
    """Request-body fields for this call, by the last look at other apps' GPU use."""
    value = watts(_wanted(current_tier())) if gpu_quiet() else None
    return {} if value is None else {"gpu_power_limit_w": value}


async def checked_payload_fields(lift: Callable[[], Awaitable[bool | None]]) -> dict[str, int]:
    """payload_fields() after a fresh look at other apps' GPU use. OMLXClient
    awaits it as each managed request is built; ``lift`` asks its engine to lift
    the cap (see "calls in flight")."""
    if watts(_wanted(current_tier())) is None:
        return {}
    if _lift_ok is None:
        answer = await _ask_lift(lift)
        _note_lift(answer)
        if answer is not True:
            return {}
    if not await _look():
        return {}
    return payload_fields()
