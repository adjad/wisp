"""Idle-model tracking: when was each resident model last actually used, and
how long to wait before auto-unloading an idle one.

Pure state, no client dependency — `service.inference.omlx_client` calls
`touch()` on every successful generation without risking a circular import.
The actual unload loop (which DOES need the client) lives in
`service.idle_unloader`, started once from `main.py`.
"""
from __future__ import annotations

import time

from service.config import models_config

_last_used: dict[str, float] = {}
# Per-model count of generations currently in flight. A model with count > 0 is
# actively serving a request (which may run for minutes — e.g. a large HTML
# render at ~20 tok/s) and MUST NOT be unloaded, even if its last_used clock has
# aged past the idle cutoff: the clock only advances at request boundaries, so a
# single long generation would otherwise look "idle" and get evicted mid-stream.
_in_flight: dict[str, int] = {}
# 0 (or less) disables auto-unload. Config value is the startup default;
# runtime changes via /idle_timeout are in-memory only, matching how
# read_only/full_access already behave — a restart reverts to the config file.
_idle_minutes: float = float(models_config().get("idle_unload_minutes", 20))


def touch(model: str) -> None:
    """Record that `model` was just used for real generation."""
    _last_used[model] = time.monotonic()


def begin(model: str) -> None:
    """Mark a generation as starting on `model`.

    Bumps the in-flight count AND touches the clock so the model is fresh at
    request start, not only at completion. Pair with `end()` in a finally.
    """
    _in_flight[model] = _in_flight.get(model, 0) + 1
    _last_used[model] = time.monotonic()


def end(model: str) -> None:
    """Mark a generation as finished on `model` (pairs with `begin`)."""
    remaining = _in_flight.get(model, 0) - 1
    if remaining > 0:
        _in_flight[model] = remaining
    else:
        _in_flight.pop(model, None)
    _last_used[model] = time.monotonic()


def in_flight(model: str) -> int:
    """How many generations are currently running on `model`."""
    return _in_flight.get(model, 0)


def last_used(model: str) -> float | None:
    return _last_used.get(model)


def forget(model: str) -> None:
    _last_used.pop(model, None)


# --------------------------------------------------------------------------
# Foreground gate: is the USER waiting on a turn right now?
#
# Wisp runs real model work in the background — the daily brief, and a profile
# refresh every 2 hours whose builder is ~50 sequential calls that hold the
# model for minutes. Nothing stopped that landing in the middle of a user's
# request, and there is only ONE resident model, so it doesn't interleave, it
# CONTENDS.
#
# Measured 2026-08-09, with the packaged app's schedulers live: the same
# summarize call took 12.8s and 319.7s on consecutive runs, and oMLX's log shows
# why — `adaptive_prefill_throttle`, `Running prefill LRU eviction`, and
# `Reclaimed 710MB of pooled Metal buffers` interleaved with tiny-prompt
# completions (prompt=71, prompt=74) that belong to a background job, not to the
# turn. A user typing during a profile rotation waits minutes for an answer that
# normally takes seconds.
#
# This is deliberately a COUNTER, not a bool: turns can overlap (a scheduled
# send firing while the user types), and a bool would let the first one to
# finish re-open the gate while the second is still running.
_foreground_turns = 0


def begin_foreground() -> None:
    """A user-facing turn has started — background model work should stand down."""
    global _foreground_turns
    _foreground_turns += 1


def end_foreground() -> None:
    global _foreground_turns
    _foreground_turns = max(0, _foreground_turns - 1)


def foreground_busy() -> bool:
    """True while any user-facing turn is in flight.

    Background jobs check this and SKIP rather than queue: they are all
    periodic, so the next tick picks the work up, and a queued job would just
    land on the user's next turn instead of this one.
    """
    return _foreground_turns > 0


def get_idle_minutes() -> float:
    return _idle_minutes


def set_idle_minutes(minutes: float) -> None:
    global _idle_minutes
    _idle_minutes = max(0.0, minutes)
