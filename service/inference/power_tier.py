"""Low Power Mode: per-request GPU power caps on Wisp's own model calls.

oMLX (0.7.3+) takes a per-request ``gpu_power_limit_w`` and holds a firmware
GPU power cap while that request generates. The cap is a limit on the whole
Mac's GPU, not just Wisp's, so Wisp only ever asks for it while the user has
Low Power Mode on (the notch toggle; off by default), and only per request:
never as a model-level setting, which would cap every request on the engine.

Measured on the M5 Pro with the Gemma E4B roster (2026-10-27, real capped
requests; the admin benchmark never applies the cap):

    cap    first token   decode   GPU J/token
    none   1.16 s        84 t/s   0.270
    20 W   +8%           -2%      -21%
    15 W   +25%          -5%      -36%
    10 W   +35%          -16%     -52%

With Low Power Mode on:

- ``CHAT`` (20 W): every call made outside a tier: Ask turns, voice, briefs
  the user opens. Nearly free in latency.
- ``BACKGROUND`` (10 W): work nobody is waiting on (gists, proactive
  triage/judge/prepare, the mail memory walk, behavior phrasing, memory
  capture).
- ``INTENSIVE`` (15 W): heavier background work whose result the user reads
  later (the scheduled Daily Summary, research).

The cap is meant to be in force only while one of these requests generates.
Upstream oMLX holds it 15 s past the last capped request. Releasing it at once
DEPENDS on the engine patch ``GpuPowerLimiter(release_delay_s=0.0)`` in
server.py (branch claude/engine-gpu-cap-no-linger, shipping in the same batch);
without that patch every capped call still trails a 15 s cap.

oMLX holds the lowest cap among active requests, so a chat that overlaps a
background call runs at the background cap until that call ends:

- cancelled in flight when a turn starts: mail_gists (gists, inbox summary,
  proactive triage/judge/prepare), the mail memory walk, the scheduled Daily
  Summary;
- checked only before the call, which then runs to completion: the behavior
  phraser (6 s timeout), memory capture (at most 650 tokens);
- research waits for a turn to end at each call boundary but does not cancel
  the call already running.

A tier is set with ``with power_tier(BACKGROUND):`` around the call (it is a
ContextVar, so tasks created inside inherit it). OMLXClient adds the field only
for the managed local engine, never for a remote provider.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from service.paths import MOE_DIR

CHAT = "chat"
BACKGROUND = "background"
INTENSIVE = "intensive"

_DEFAULT_WATTS = {CHAT: 20, BACKGROUND: 10, INTENSIVE: 15}
# 0 turns a tier's cap off; anything outside oMLX's 2-1000 W range is ignored.
_ENV = {CHAT: "WISP_CHAT_GPU_W", BACKGROUND: "WISP_BACKGROUND_GPU_W",
        INTENSIVE: "WISP_INTENSIVE_GPU_W"}
_MIN_W, _MAX_W = 2, 1000

SETTINGS_PATH = MOE_DIR / "power" / "settings.json"

_tier: ContextVar[str | None] = ContextVar("wisp_power_tier", default=None)


# --- the setting ------------------------------------------------------------------
#
# The switch turns itself off (and the stored value becomes off) when:
# - Wisp starts: reset_at_launch() runs in the backend's startup and the app
#   asks again at launch, so a crash or force-quit also ends it;
# - the user hasn't used Wisp for IDLE_OFF_S: no turn, no notch or window
#   opened (service/idle.py's user clock; background work never counts);
# - another app is launched or brought to the front while no turn is running
#   (other_app(), from the app's NSWorkspace didLaunchApplication and
#   didActivateApplication observers; the app ignores Wisp, oMLX and system UI).
# A call already sent keeps the cap it was sent with until it ends, exactly as
# after a manual off; every later call goes uncapped.

IDLE_OFF_S = 600.0

log = logging.getLogger("wisp.power")


def _stored(path: Path) -> bool:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or unreadable means off
        return False
    return isinstance(data, dict) and data.get("low_power") is True


def low_power(path: Path | None = None) -> bool:
    """Whether Low Power Mode is on. Off when unset or unreadable: a cap
    throttles the whole Mac, so it is never applied without a clear yes.
    Reading it also applies the idle auto-off."""
    path = SETTINGS_PATH if path is None else path
    if not _stored(path):
        return False
    from service import idle
    if idle.user_idle_seconds() >= IDLE_OFF_S:
        auto_off(f"Wisp unused for {IDLE_OFF_S / 60:.0f} minutes", path)
        return False
    return True


def auto_off(reason: str, path: Path | None = None) -> bool:
    """Turn Low Power Mode off if it is on, logging why. True when it changed."""
    path = SETTINGS_PATH if path is None else path
    if not _stored(path):
        return False
    set_low_power(False, path)
    log.info("Low Power Mode turned off: %s", reason)
    return True


def reset_at_launch(path: Path | None = None) -> bool:
    return auto_off("Wisp started", path)


OTHER_APP_EVENTS = {"launched": "another app was opened while Wisp was idle",
                    "activated": "you switched to another app while Wisp was idle"}


# An app event that arrived during a turn, applied when the turn ends.
_pending_other_app: str | None = None


def other_app(event: str, path: Path | None = None) -> bool:
    """Another app was launched or brought to the front: off, or once the
    current turn ends if the user is in one."""
    global _pending_other_app
    if event not in OTHER_APP_EVENTS:
        raise ValueError(f"unknown app event: {event!r}")
    from service import idle
    if idle.foreground_busy():
        _pending_other_app = event
        return False
    _pending_other_app = None
    return auto_off(OTHER_APP_EVENTS[event], path)


def turn_ended(path: Path | None = None) -> bool:
    """The last turn ended (service/idle.py): apply an app event held during it."""
    global _pending_other_app
    event, _pending_other_app = _pending_other_app, None
    return event is not None and auto_off(OTHER_APP_EVENTS[event], path)


def set_low_power(on: bool, path: Path | None = None) -> bool:
    if type(on) is not bool:
        raise ValueError("low_power must be true or false")
    path = SETTINGS_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"low_power": on}, f)
    tmp.replace(path)
    return on


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
    """Request-body fields for this call: none unless Low Power Mode is on.

    A call outside any tier is a live call, so it gets the chat cap."""
    if not low_power():
        return {}
    value = watts(current_tier() or CHAT)
    return {} if value is None else {"gpu_power_limit_w": value}
