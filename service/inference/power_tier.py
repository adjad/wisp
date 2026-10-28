"""GPU power tiers for background model calls.

oMLX (0.7.3+) takes a per-request ``gpu_power_limit_w`` and holds a firmware
GPU power cap while that request generates. Measured on the M5 Pro with the
Gemma E4B roster (2026-10-27, real capped requests; the admin benchmark never
applies the cap):

    cap    first token   decode   GPU J/token
    none   1.16 s        84 t/s   0.270
    15 W   +25%          -5%      -36%
    10 W   +35%          -16%     -52%
    5 W    +121%         -42%     -63%

Background work nobody is waiting on runs at ``BACKGROUND`` (10 W). Heavier
background work whose result the user reads later (the scheduled Daily
Summary, research jobs) runs at ``INTENSIVE`` (15 W). Live chat turns carry no
tier, so they run at whatever the engine's own model setting allows.

The cap is system-wide and oMLX holds the lowest cap among active requests,
then keeps it for 15 s after the last capped request ends, so a chat that
overlaps a tiered call runs at that call's cap until the call ends. How long
that lasts depends on the caller:

- cancelled in flight when a turn starts: mail_gists (gists, inbox summary,
  proactive triage/judge/prepare), the mail memory walk, the scheduled Daily
  Summary;
- checked only before the call, which then runs to completion: the behavior
  phraser (6 s timeout), memory capture (at most 650 tokens);
- research waits for a turn to end at each call boundary but does not cancel
  the call already running.

A model-level cap above both tiers (Gemma's engine profile sets 20 W)
replaces a lingering tier cap as soon as the chat's own request starts.

A tier is set with ``with power_tier(BACKGROUND):`` around the call (it is a
ContextVar, so tasks created inside inherit it). OMLXClient adds the field only
for the managed local engine, never for a remote provider.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

BACKGROUND = "background"
INTENSIVE = "intensive"

_DEFAULT_WATTS = {BACKGROUND: 10, INTENSIVE: 15}
# 0 turns a tier's cap off; anything outside oMLX's 2-1000 W range is ignored.
_ENV = {BACKGROUND: "WISP_BACKGROUND_GPU_W", INTENSIVE: "WISP_INTENSIVE_GPU_W"}
_MIN_W, _MAX_W = 2, 1000

_tier: ContextVar[str | None] = ContextVar("wisp_power_tier", default=None)


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
    """Request-body fields for the current tier (empty outside any tier)."""
    value = watts(current_tier())
    return {} if value is None else {"gpu_power_limit_w": value}
