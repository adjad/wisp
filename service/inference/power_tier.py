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
- A cap must never slow another app. macOS has no per-app GPU cap, so Wisp
  caps a call only while no other app is using the GPU, looks again right
  before every capped request, keeps looking every WATCH_EVERY_S while one
  runs, and lifts the cap the moment another app starts (see "other apps on
  the GPU" below). Any doubt (a failed read, no recent reading) means uncapped.
- Only Wisp's bundled engine is ever capped. It drops the cap the moment the
  last capped request ends (``GpuPowerLimiter(release_delay_s=0.0)``, since
  0.5.0-dev6) and whenever Wisp asks, even mid-request (``POST
  /v1/wisp/gpu-power/lift``, build-support/engine_guard, since 0.5.0-dev7). The
  user's own oMLX.app holds a cap 15 s past its request and can't lift one, so
  it never gets a cap.
- A call someone is waiting on never runs under a lower cap than its own: one
  starting while a lower-capped call is in flight lifts that cap.

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
- ``BACKGROUND`` (the mail memory walk, the behavior tick's interpreter and
  phraser, memory capture): 10 W on battery or while the user is away (no
  keyboard or mouse input for AWAY_AFTER_S and no turn running); uncapped
  otherwise.
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
from collections import Counter
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
    return save_when_away() and _lift_ok is not False and not _remote_only() and _bundled_engine()


def _on_battery_probe() -> bool:
    from service.mail_memory.runner import _cached, _power
    on_ac, _percent, _low_power = _cached("power", _power)   # a failed read says AC
    return not on_ac


def refresh_away(hid_idle_s: float | None = None, on_battery: bool | None = None, *,
                 probe: bool = True) -> bool:
    """Read the input idle time and the power source, and store whether the
    user is away and whether the Mac is on battery. While either could cap a
    call, also read other apps' GPU use, so the look before each capped request
    compares against a reading at most a tick old. Blocking: call it from a
    worker thread. Returns away.

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
    if idle_enough or battery:
        check_other_gpu()
    else:
        drop_gpu_readings()                      # read only while a cap could apply
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
# too. check_other_gpu reads every process's accumulated GPU time (ioreg
# AGXDeviceUserClient AppUsage, readable without privileges, ~15 ms) and
# compares it with the previous read: if other apps used at least
# OTHER_GPU_BUSY of the GPU in between, nothing is capped. It runs on the
# scheduler tick while a cap could apply, right before each capped request,
# and every WATCH_EVERY_S while one runs. Wisp itself (the app and its bundled
# engine) and WindowServer's compositing don't count; the user's oMLX.app does,
# since it is never Wisp's capped engine. No baseline, a baseline older than
# _GPU_BASELINE_MAX_S, or a failed read counts as busy (uncapped).

OTHER_GPU_BUSY = 0.02
WATCH_EVERY_S = 1.0
_GPU_MIN_GAP_S = 0.5           # a shorter window can't tell: reuse the last answer
_GPU_BASELINE_MAX_S = 120.0
_GPU_FRESH_S = 2.0             # a cap needs an answer at most this old
_OWN_PATH_PARTS = ("/Wisp.app/",)
_gpu_lock = threading.Lock()
_gpu_sample: tuple[float, dict[int, int]] | None = None    # (monotonic, {pid: GPU ns})
_gpu_answer: tuple[float, float, bool] | None = None       # (monotonic, wall, quiet)
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


def check_other_gpu() -> bool:
    """Read other apps' GPU time now and store whether they left the GPU quiet
    since the previous read. Blocking (an ioreg subprocess): call it from a
    worker thread. Returns quiet."""
    global _gpu_sample, _gpu_answer
    with _gpu_lock:
        previous = _gpu_sample
        if (previous is not None and _gpu_answer is not None
                and time.monotonic() - previous[0] < _GPU_MIN_GAP_S):
            return _gpu_answer[2]
        times = _other_gpu_times()
        now = time.monotonic()
        _gpu_sample = None if times is None else (now, times)
        quiet = False
        if times is not None and previous is not None and 0 < now - previous[0] <= _GPU_BASELINE_MAX_S:
            then, before = previous
            used_ns = sum(max(0, ns - before.get(pid, 0)) for pid, ns in times.items())
            quiet = used_ns / ((now - then) * 1e9) < OTHER_GPU_BUSY
        _gpu_answer = (now, time.time(), quiet)
        return quiet


async def _look() -> bool:
    """check_other_gpu off the event loop; an error counts as busy."""
    try:
        return await asyncio.to_thread(check_other_gpu)
    except Exception:  # noqa: BLE001
        return False


def drop_gpu_readings() -> None:
    global _gpu_sample, _gpu_answer
    with _gpu_lock:
        _gpu_sample = _gpu_answer = None


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
# lower-capped one. The first capped request of a run asks once beforehand, so
# an engine that can't lift (an older bundled engine) is never capped at all.

_watched_calls = 0
_capped: Counter[int] = Counter()         # the caps of the capped calls in flight, in watts
_lift: Callable[[], Awaitable[bool | None]] | None = None
_lift_ok: bool | None = None              # None: not asked yet this run
_lifted = False                           # the watch lifted the caps in flight already
_watch_task: asyncio.Task | None = None
_lift_tasks: set[asyncio.Task] = set()


async def _ask_lift(lift: Callable[[], Awaitable[bool | None]]) -> bool | None:
    """True: the engine lifted the cap. False: it can't (no such route). None: it
    couldn't be asked (not reachable)."""
    try:
        return await lift()
    except Exception:  # noqa: BLE001 — treated as unreachable
        return None


async def _lift_now() -> None:
    """Lift every cap. Cheap and idempotent on the engine, so a call someone
    waits on always asks; only the watch skips a cap it lifted already."""
    global _lifted, _lift_ok
    if _lift is None:
        return
    _lifted = True
    if await _ask_lift(_lift) is not True:
        _lift_ok = False                     # it can't be lifted: cap nothing more this run
        logger.warning("The engine could not lift Wisp's GPU power cap; no more caps this run")


def _schedule_lift() -> None:
    task = asyncio.get_running_loop().create_task(_lift_now())
    _lift_tasks.add(task)
    task.add_done_callback(_lift_tasks.discard)


async def _watch() -> None:
    """While capped calls run, look at other apps' GPU use every WATCH_EVERY_S.
    A capped call starts only after a quiet look, and re-arms the watch."""
    global _watch_task
    try:
        while _capped:
            await asyncio.sleep(WATCH_EVERY_S)
            if _capped and not _lifted and not await _look():
                await _lift_now()
    finally:
        _watch_task = None


def call_started(watts: int | None = None) -> tuple[bool, int | None]:
    """OMLXClient, as a managed request goes out with ``watts`` as its cap (None
    for none). Pass the result to call_ended. Counts calls someone is waiting on
    (anything but BRIEF and BACKGROUND) and capped calls, watches while capped
    ones run, and lifts a lower cap a watched call would otherwise run under."""
    global _watched_calls, _lifted, _watch_task
    watched = current_tier() not in _UNWATCHED
    if watched:
        _watched_calls += 1
        if _capped and min(_capped) < (watts or _MAX_W + 1):
            _schedule_lift()
    if watts is not None:
        _capped[watts] += 1
        _lifted = False                      # a new cap is set
        if _watch_task is None:
            _watch_task = asyncio.get_running_loop().create_task(_watch())
    return watched, watts


def call_ended(started: tuple[bool, int | None]) -> None:
    global _watched_calls
    watched, watts = started
    if watched:
        _watched_calls = max(0, _watched_calls - 1)
    if watts is not None:
        _capped[watts] -= 1
        if _capped[watts] <= 0:
            del _capped[watts]


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
    """The cap a call at ``tier`` gets right now if no other app is on the GPU."""
    if tier not in _TIERS or not _capping_possible():
        return None
    if tier in _UNWATCHED and watched_running():
        return None
    if tier in _UNWATCHED and away():
        return LOW
    if tier == CHAT and not _turn_running():
        return None
    if tier == BACKGROUND and on_battery():
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
    global _lift, _lift_ok
    if watts(_wanted(current_tier())) is None:
        return {}
    if _lift_ok is None:
        answer = await _ask_lift(lift)
        if answer is None:
            return {}                        # ask again next time
        _lift_ok = answer
        if not answer:
            logger.warning("The engine can't lift a GPU power cap; Wisp won't cap it")
            return {}
    _lift = lift
    await _look()
    return payload_fields()
