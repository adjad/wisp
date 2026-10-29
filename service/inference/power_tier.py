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

Every cap needs the "Save power" switch on (the notch; on by default; stored as
``save_when_away``) and no other app busy on the GPU (a game, an export, a
render: the cap would throttle it too). Then:

- ``CHAT`` (chat replies: Ask turns typed, spoken or from the phone, with the
  history folding at the end of a turn, and questions about an attached image
  or document): 15 W while the Mac is on battery and the turn is still
  running; uncapped on AC. "On battery" is pmset's "Battery Power"; a desktop
  drawing from a UPS reports "UPS Power" and counts as battery too.
- ``BRIEF`` (Brief generation: the mail and texts gists, the inbox summary, the
  proactive triage/judge/prepare behind its cards): 15 W on battery, and 10 W
  while the user is away; uncapped otherwise.
- ``BACKGROUND`` (the mail memory walk, the behavior tick's interpreter and
  phraser, memory capture): 10 W while the user is away (no keyboard or mouse
  input for AWAY_AFTER_S and no turn running); uncapped otherwise.
- Everything else is never capped: research, connection investigations the
  user asks for (memory/connections.py), voice transcription, setup probes.

The Daily Summary, scheduled or opened, makes no model call
(service/assistant/brief.py renders it from caches), so there is nothing to cap.

While any non-background call is in flight (chat, research, ...), BRIEF and
BACKGROUND calls go uncapped, so they can't drag it below its own cap. One
already in flight keeps its cap until it ends; most are cancelled when a turn
starts (mail_gists, the mail memory walk), and the rest are short.

The cap is released the moment a request ends by Wisp's bundled engine, which
carries ``GpuPowerLimiter(release_delay_s=0.0)`` in server.py since 0.5.0-dev6.
A standalone oMLX.app serving Wisp keeps upstream's 15 s release delay, so
there a call started within 15 s of a capped one still runs capped until then.

A tier is set with ``with power_tier(CHAT):`` around the call (it is a
ContextVar, so tasks created inside inherit it). OMLXClient adds the field only
for the managed local engine, never for a remote provider.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from service.paths import MOE_DIR

CHAT = "chat"
BRIEF = "brief"
BACKGROUND = "background"
_TIERS = (CHAT, BRIEF, BACKGROUND)
_UNWATCHED = (BRIEF, BACKGROUND)          # nobody is waiting on these

# The two caps: while the user is away, and while the Mac is on battery.
AWAY = "away"
BATTERY = "battery"
_DEFAULT_WATTS = {AWAY: 10, BATTERY: 15}
# 0 turns the cap off; anything outside oMLX's 2-1000 W range is ignored.
_ENV = {AWAY: "WISP_BACKGROUND_GPU_W", BATTERY: "WISP_BATTERY_GPU_W"}
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


def _on_battery_probe() -> bool:
    from service.mail_memory.runner import _cached, _power
    on_ac, _percent, _low_power = _cached("power", _power)   # a failed read says AC
    return not on_ac


def refresh_away(hid_idle_s: float | None = None, on_battery: bool | None = None, *,
                 probe: bool = True) -> bool:
    """Read the input idle time and the power source, and store whether the
    user is away and whether the Mac is on battery (each only while the GPU is
    otherwise quiet). Blocking: call it from a worker thread. Returns away.

    With the switch off, or no local engine to cap (a remote-only profile),
    nothing could be capped, so nothing is read: no ioreg, no pmset."""
    global _away, _battery
    if not save_when_away() or _remote_only():
        _gpu_quiet(sample=False)                 # drops the GPU baseline
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
    quiet = _gpu_quiet(sample=idle_enough or battery)
    mono, wall = time.monotonic(), time.time()
    _away = (mono, wall, idle_enough and quiet)
    _battery = (mono, wall, battery and quiet)
    return _away[2]


def _fresh(reading: tuple[float, float, bool] | None) -> bool:
    if reading is None:
        return False
    mono, wall, value = reading
    return value and time.monotonic() - mono < _AWAY_STALE_S and 0 <= time.time() - wall < _AWAY_STALE_S


# --- other apps on the GPU --------------------------------------------------------
#
# The cap limits the whole Mac's GPU, so a game, video export or 3D render
# running at the same time would be throttled too. While the input is idle or
# the Mac is on battery, each refresh reads every process's accumulated GPU time (ioreg
# AGXDeviceUserClient AppUsage, readable without privileges) and compares it with
# the previous refresh: if other apps used at least OTHER_GPU_BUSY of the GPU in
# between, nothing is capped. Wisp, its engine (the user's oMLX.app
# or the engine bundled in Wisp.app) and WindowServer's compositing don't count.
# No baseline yet, a failed read, or a too-short gap counts as busy (uncapped).

OTHER_GPU_BUSY = 0.10
_OWN_PATH_PARTS = ("/oMLX.app/", "/Wisp.app/")
_gpu_sample: tuple[float, dict[int, int]] | None = None   # (monotonic, {pid: GPU ns})
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


def _own(pid: int) -> bool:
    """Wisp, its engine, or WindowServer. An unreadable path counts as another app."""
    from service.inference.local_peer import process_path
    try:
        path = process_path(pid)
    except Exception:  # noqa: BLE001 — a process that exited or can't be read
        return False
    return any(part in path for part in _OWN_PATH_PARTS) or path.endswith("/WindowServer")


def _other_gpu_times() -> dict[int, int] | None:
    try:
        raw = subprocess.run(["/usr/sbin/ioreg", "-r", "-c", "AGXDeviceUserClient", "-l", "-w0"],
                             capture_output=True, text=True, errors="replace", timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return {pid: ns for pid, ns in _parse_gpu_times(raw).items() if not _own(pid)}


def _gpu_quiet(sample: bool) -> bool:
    """Whether other apps left the GPU quiet since the previous refresh."""
    global _gpu_sample
    if not sample:
        _gpu_sample = None                       # sampled only when a cap could apply
        return False
    times, now = _other_gpu_times(), time.monotonic()
    previous, _gpu_sample = _gpu_sample, (None if times is None else (now, times))
    if times is None or previous is None or now - previous[0] < 5:
        return False
    then, before = previous
    used_ns = sum(max(0, ns - before.get(pid, 0)) for pid, ns in times.items())
    return used_ns / ((now - then) * 1e9) < OTHER_GPU_BUSY


def away() -> bool:
    """Whether the user is away: idle input, a quiet GPU, a fresh reading, no turn."""
    from service import idle
    return not idle.foreground_busy() and _fresh(_away)


def on_battery() -> bool:
    """Whether the Mac is on battery, with a quiet GPU and a fresh reading."""
    return _fresh(_battery)


# --- calls someone is waiting on, in flight ---------------------------------------

_watched_calls = 0


def call_started() -> bool:
    """OMLXClient, as a managed call starts. True when it is a call someone is
    waiting on (anything but BRIEF and BACKGROUND); pass that to call_ended."""
    global _watched_calls
    if current_tier() in _UNWATCHED:
        return False
    _watched_calls += 1
    return True


def call_ended(watched: bool) -> None:
    global _watched_calls
    if watched:
        _watched_calls = max(0, _watched_calls - 1)


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
    """The AWAY or BATTERY cap in watts, or None for no cap."""
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


def _cap(tier: str | None) -> str | None:
    """Which cap applies to a call at ``tier`` right now, if any."""
    if tier not in _TIERS or not save_when_away():
        return None
    if tier in _UNWATCHED and watched_running():
        return None
    if tier in _UNWATCHED and away():
        return AWAY
    if tier == CHAT and not _turn_running():
        return None
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
    """Request-body fields for this call (see the module docstring)."""
    value = watts(_cap(current_tier()))
    return {} if value is None else {"gpu_power_limit_w": value}
