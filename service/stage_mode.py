"""Demo mode: answer as fast as this Mac can, for a live demo.

The menu calls it "Demo mode"; in code it is stage mode. While it is on:
- no GPU power cap for any call (service/inference/power_tier.py);
- the chat model is never parked or idle-unloaded, and Save memory counts as off
  (service/memory_saver.py, service/idle_unloader.py);
- heavy scheduled background model work waits (see `holds_background`), so
  nothing else is on the GPU when the person asks; the proactive cycle that
  reads a new text and acts on it still runs;
- the chat model is loaded and its prompt prefix warmed at once (service/stage_warm.py).

IMPORTANT rules:
- The app is the authority. This is memory only: never a file, never a saved
  setting, so nothing a shell can write turns it on, and a restart of the
  service starts with it off (the app pushes it again if it is still on there).
- It changes none of the person's saved choices (Save power, Save memory, the
  idle-unload minutes). Each of those is only read as "off" while this is on;
  turning it off puts everything back exactly as it was.
- It ends by itself after MAX_S, so a forgotten switch does not drain a battery.
  The app keeps its own timer; this is the backstop.
- Every reader calls `active()` each time. Nothing caches it.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

MAX_S = 4 * 60 * 60

_lock = threading.Lock()
_since: float | None = None            # time.monotonic() when it was turned on; None = off
_listeners: list[Callable[[bool], None]] = []


def active(now: float | None = None) -> bool:
    """On, and not past its four hours."""
    since = _since
    if since is None:
        return False
    return (time.monotonic() if now is None else now) - since < MAX_S


def holds_background() -> bool:
    """Heavy scheduled model work (mail memory, digests, summaries, memory extraction) waits
    while this is True. NOT for the proactive cycle or anything the person just asked for."""
    return active()


def remaining_s(now: float | None = None) -> float:
    since = _since
    if since is None:
        return 0.0
    return max(0.0, MAX_S - ((time.monotonic() if now is None else now) - since))


def on_change(listener: Callable[[bool], None]) -> None:
    """Call `listener(on)` after every change (the warm-up registers here). Never raises."""
    if listener not in _listeners:
        _listeners.append(listener)


def set_active(on: bool, now: float | None = None) -> dict[str, Any]:
    """Turn it on or off. Turning it on while it is on keeps the time it started, so pushing
    the same state again (the app does, when the service comes back) never extends it."""
    global _since
    if type(on) is not bool:
        raise ValueError("enabled must be true or false")
    with _lock:
        was = active(now)
        if on and not was:
            _since = time.monotonic() if now is None else now
        elif not on:
            _since = None
        changed = was != on
    if changed:
        try:
            from service.safety.audit import audit
            audit("stage_mode", enabled=on)
        except Exception:  # noqa: BLE001 — auditing never blocks the switch
            pass
        for listener in list(_listeners):
            try:
                listener(on)
            except Exception:  # noqa: BLE001 — a failed warm-up never fails the switch
                pass
    return state(now)


def state(now: float | None = None) -> dict[str, Any]:
    on = active(now)
    return {"enabled": on, "remaining_s": int(remaining_s(now)) if on else 0, "max_s": MAX_S}
