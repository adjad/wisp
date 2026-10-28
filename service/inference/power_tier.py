"""Per-request GPU power caps on Wisp's own model calls.

oMLX (0.7.3+) takes a per-request ``gpu_power_limit_w`` and holds a firmware
GPU power cap while that request generates. The cap is a limit on the whole
Mac's GPU, not just Wisp's, and oMLX holds the LOWEST cap among the requests
in flight. So Wisp only ever asks per request, never as a model-level setting
(which would cap every request on the engine, intensive work included).

Measured on the M5 Pro with the Gemma E4B roster (2026-10-27, real capped
requests; the admin benchmark never applies the cap):

    cap    first token   decode   GPU J/token
    none   1.16 s        84 t/s   0.270
    20 W   +8%           -2%      -21%
    10 W   +35%          -16%     -52%

The tiers:

- ``CHAT`` (20 W, always): every call made outside a tier: Ask turns, voice,
  briefs the user opens. Nearly free in latency.
- ``BACKGROUND`` (10 W): work nobody is waiting on (gists, proactive
  triage/judge/prepare, the mail memory walk, behavior phrasing, memory
  capture). Capped only while the "Save power when away" switch is on (the
  notch; on by default) AND the user is away: no keyboard or mouse input for
  AWAY_AFTER_S and no turn running. With someone at the Mac it runs uncapped.
- ``INTENSIVE`` (never capped): heavier work that needs the GPU (the scheduled
  Daily Summary, research). While one runs, background calls go uncapped too,
  because oMLX would otherwise hold the intensive call to their 10 W. A
  background call already in flight keeps its cap until it ends.

Releasing the cap the moment a request ends DEPENDS on the engine patch
``GpuPowerLimiter(release_delay_s=0.0)`` in server.py (branch
claude/engine-gpu-cap-no-linger); upstream oMLX holds it 15 s longer.

A tier is set with ``with power_tier(BACKGROUND):`` around the call (it is a
ContextVar, so tasks created inside inherit it). OMLXClient adds the field only
for the managed local engine, never for a remote provider.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from service.paths import MOE_DIR

CHAT = "chat"
BACKGROUND = "background"
INTENSIVE = "intensive"

_DEFAULT_WATTS = {CHAT: 20, BACKGROUND: 10, INTENSIVE: None}
# 0 turns a tier's cap off; anything outside oMLX's 2-1000 W range is ignored.
# INTENSIVE has no override: it is never capped.
_ENV = {CHAT: "WISP_CHAT_GPU_W", BACKGROUND: "WISP_BACKGROUND_GPU_W"}
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
# the mail memory walk uses). Reading it runs ioreg, so it is never read on a
# request: the scheduler refreshes it off the event loop every tick, and a
# reading older than _AWAY_STALE_S counts as present (uncapped).

AWAY_AFTER_S = 600.0
_AWAY_STALE_S = 120.0
_away: tuple[float, bool] | None = None      # (monotonic time read, away)


def refresh_away(hid_idle_s: float | None = None, *, probe: bool = True) -> bool:
    """Read the input idle time and store whether the user is away. Blocking:
    call it from a worker thread."""
    global _away
    if probe and hid_idle_s is None:
        from service.mail_memory.runner import _hid_idle_s
        hid_idle_s = _hid_idle_s()
    value = hid_idle_s is not None and hid_idle_s >= AWAY_AFTER_S
    _away = (time.monotonic(), value)
    return value


def away() -> bool:
    """Whether the user is away: idle input, a fresh reading, and no turn."""
    from service import idle
    if idle.foreground_busy() or _away is None:
        return False
    read_at, value = _away
    return value and time.monotonic() - read_at < _AWAY_STALE_S


# --- intensive work in flight -----------------------------------------------------

_intensive_calls = 0


def call_started() -> bool:
    """OMLXClient, as a managed call starts. True when it is intensive work;
    pass that to call_ended."""
    global _intensive_calls
    if current_tier() != INTENSIVE:
        return False
    _intensive_calls += 1
    return True


def call_ended(intensive: bool) -> None:
    global _intensive_calls
    if intensive:
        _intensive_calls = max(0, _intensive_calls - 1)


def intensive_running() -> bool:
    return _intensive_calls > 0


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
    if tier not in _DEFAULT_WATTS or _DEFAULT_WATTS[tier] is None:
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
    """Request-body fields for this call. A call outside any tier is a live
    call, so it gets the chat cap."""
    tier = current_tier() or CHAT
    if tier == BACKGROUND and not (save_when_away() and away() and not intensive_running()):
        return {}
    value = watts(tier)
    return {} if value is None else {"gpu_power_limit_w": value}
