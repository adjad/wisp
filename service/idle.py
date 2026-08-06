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


def get_idle_minutes() -> float:
    return _idle_minutes


def set_idle_minutes(minutes: float) -> None:
    global _idle_minutes
    _idle_minutes = max(0.0, minutes)
