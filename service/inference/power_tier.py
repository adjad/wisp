"""A GPU power cap on Wisp's background model calls while the user is away.

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
    10 W   +35%          -16%     -52%

Only ``BACKGROUND`` work is ever capped (10 W): work nobody is waiting on
(gists and the inbox summary, proactive triage/judge/prepare, the mail memory
walk, the behavior tick's interpreter and phraser, memory capture). And only
while the "Save power when away" switch is on (the notch; on by default) AND
the user is away: no keyboard or mouse input for AWAY_AFTER_S, no turn
running, and no other app busy on the GPU (a game, an export, a render: the
cap would throttle it too). With someone at the Mac it runs uncapped.

Everything else is never capped: Ask turns (and the history folding at the
end of a turn, memory/context.maybe_summarize), voice, phone turns, research,
connection investigations the user asks for (memory/connections.py), a brief
the user opens, setup probes. While any of those is in flight,
background calls go uncapped too, so they can't drag it down to 10 W. A
background call already in flight keeps its cap until it ends; most of them
are cancelled when a turn starts (mail_gists, the mail memory walk), and the
rest are short (the phraser, memory capture).

The scheduled Daily Summary makes no model call (service/assistant/brief.py
renders it from caches), so it has no tier; the model-written parts of the
Brief come from the background gists and proactive cards.

Releasing the cap the moment a request ends DEPENDS on the engine patch
``GpuPowerLimiter(release_delay_s=0.0)`` in server.py (branch
claude/engine-gpu-cap-no-linger); upstream oMLX holds it 15 s longer, so a
reply started within 15 s of a capped call still runs capped until then.

A tier is set with ``with power_tier(BACKGROUND):`` around the call (it is a
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

BACKGROUND = "background"

_DEFAULT_WATTS = {BACKGROUND: 10}
# 0 turns the cap off; anything outside oMLX's 2-1000 W range is ignored.
_ENV = {BACKGROUND: "WISP_BACKGROUND_GPU_W"}
_MIN_W, _MAX_W = 2, 1000

SETTINGS_PATH = MOE_DIR / "power" / "settings.json"
_KEY = "save_when_away"

_tier: ContextVar[str | None] = ContextVar("wisp_power_tier", default=None)


# --- the switch -------------------------------------------------------------------

def save_when_away(path: Path | None = None) -> bool:
    """The "Save power when away" switch. On unless the user turned it off: it
    only ever caps background work while nobody is at the Mac."""
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


# --- away -------------------------------------------------------------------------
#
# Away is read from the keyboard/mouse idle time (IOHIDSystem, the same probe
# the mail memory walk uses, which reuses this reading). Reading it runs ioreg,
# so it is never read on a request: the scheduler refreshes it off the event
# loop every tick, and a reading older than _AWAY_STALE_S counts as present
# (uncapped). With the switch off nothing is ever capped, so nothing is read.
#
# Age is checked on both clocks. time.monotonic() stops while the Mac sleeps, so
# a reading taken just before sleep would look fresh at wake and cap the wake
# backfill while the user is back; the wall clock counts the sleep. A wall
# clock set back (a reading "from the future") also counts as stale.

AWAY_AFTER_S = 600.0
_AWAY_STALE_S = 120.0
_away: tuple[float, float, bool] | None = None   # (monotonic, wall time read, away)


def refresh_away(hid_idle_s: float | None = None, *, probe: bool = True) -> bool:
    """Read the input idle time and store whether the user is away. Blocking:
    call it from a worker thread. With "Save power when away" off it runs no
    ioreg at all and stores "present"."""
    global _away, _gpu_sample
    if probe and hid_idle_s is None:
        if not save_when_away():
            _gpu_sample = None                   # a later baseline starts fresh
            _away = (time.monotonic(), time.time(), False)
            return False
        from service.mail_memory.runner import fresh_hid_idle_s
        hid_idle_s = fresh_hid_idle_s()
    idle_enough = hid_idle_s is not None and hid_idle_s >= AWAY_AFTER_S
    value = _gpu_quiet(sample=idle_enough) and idle_enough
    _away = (time.monotonic(), time.time(), value)
    return value


# --- other apps on the GPU --------------------------------------------------------
#
# The cap limits the whole Mac's GPU, so a game, video export or 3D render left
# running while the user is away would be throttled to 10 W too. While the input
# is idle, each refresh reads every process's accumulated GPU time (ioreg
# AGXDeviceUserClient AppUsage, readable without privileges) and compares it with
# the previous refresh: if other apps used at least OTHER_GPU_BUSY of the GPU in
# between, background work stays uncapped. Wisp, its engine (the user's oMLX.app
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
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {pid: ns for pid, ns in _parse_gpu_times(raw).items() if not _own(pid)}


def _gpu_quiet(sample: bool) -> bool:
    """Whether other apps left the GPU quiet since the previous refresh."""
    global _gpu_sample
    if not sample:
        _gpu_sample = None                       # sampled only while the input is idle
        return False
    times, now = _other_gpu_times(), time.monotonic()
    previous, _gpu_sample = _gpu_sample, (None if times is None else (now, times))
    if times is None or previous is None or now - previous[0] < 5:
        return False
    then, before = previous
    used_ns = sum(max(0, ns - before.get(pid, 0)) for pid, ns in times.items())
    return used_ns / ((now - then) * 1e9) < OTHER_GPU_BUSY


def away() -> bool:
    """Whether the user is away: idle input, a fresh reading, and no turn."""
    from service import idle
    if idle.foreground_busy() or _away is None:
        return False
    mono, wall, value = _away
    return (value and time.monotonic() - mono < _AWAY_STALE_S
            and 0 <= time.time() - wall < _AWAY_STALE_S)


# --- uncapped calls in flight -----------------------------------------------------

_uncapped_calls = 0


def call_started() -> bool:
    """OMLXClient, as a managed call starts. True when it is an uncapped
    (non-background) call; pass that to call_ended."""
    global _uncapped_calls
    if current_tier() == BACKGROUND:
        return False
    _uncapped_calls += 1
    return True


def call_ended(uncapped: bool) -> None:
    global _uncapped_calls
    if uncapped:
        _uncapped_calls = max(0, _uncapped_calls - 1)


def uncapped_running() -> bool:
    return _uncapped_calls > 0


# --- tiers ------------------------------------------------------------------------

@contextmanager
def power_tier(tier: str) -> Iterator[None]:
    """Run the enclosed model calls under ``tier``'s GPU power cap."""
    if tier not in _DEFAULT_WATTS:
        raise ValueError(f"unknown power tier: {tier!r}")
    token = _tier.set(tier)
    try:
        yield
    finally:
        _tier.reset(token)


def current_tier() -> str | None:
    return _tier.get()


def watts(tier: str | None) -> int | None:
    """The cap for ``tier`` in watts, or None for no cap."""
    if tier not in _DEFAULT_WATTS:
        return None
    raw = os.environ.get(_ENV[tier], "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = _DEFAULT_WATTS[tier]
        if value == 0:
            return None
        if _MIN_W <= value <= _MAX_W:
            return value
    return _DEFAULT_WATTS[tier]


def payload_fields() -> dict[str, int]:
    """Request-body fields for this call: a cap only for background work while
    the user is away, the switch is on, and nothing uncapped is running."""
    tier = current_tier()
    if tier != BACKGROUND or not (save_when_away() and away() and not uncapped_running()):
        return {}
    value = watts(tier)
    return {} if value is None else {"gpu_power_limit_w": value}
