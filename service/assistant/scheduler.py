"""Assistant background loop.

One asyncio task (started in FastAPI lifespan, like idle_unloader). Its steady
job is the reminder engine: every tick it runs the lead-time rules over the
commitments store and publishes any due reminders to SSE subscribers.

Calendar data is fed IN by the Swift app (POST /assistant/sync/calendar) rather
than read here: macOS Calendar (TCC) permission is per-process, and the app has
the clean Wisp.app identity + Info.plist usage strings, whereas this Python
backend is a separate binary that can't get a coherent "Wisp" prompt. Future
credential-free sources (e.g. a mail IMAP connector) can still poll from here.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime

from service.assistant.hub import hub
from service.assistant.reminders import due_reminders
from service.assistant.store import assistant_store

_last_brief: date | None = None

TICK_S = 30.0

# Server-side connectors that DON'T need macOS TCC (none yet; mail lands here).
_connectors: list = []

# Last-sync bookkeeping for sources fed by the app, surfaced in /assistant/status.
_sync_status: dict[str, dict] = {
    "calendar": {"available": False, "reason": "waiting for the app to sync", "count": 0},
}


def record_sync(source: str, count: int, diagnostics: dict | None = None) -> None:
    _sync_status[source] = {
        "available": True, "reason": "ok", "count": count, "last_sync": time.time(),
        "diagnostics": diagnostics or {},
    }


def connectors_status() -> dict[str, dict]:
    return dict(_sync_status)


def request_calendar_access() -> None:
    # Calendar access is requested from the Swift side (Wisp.app identity);
    # nothing to do in the backend. Kept for API compatibility.
    return None


async def run() -> None:
    while True:
        # server-side connectors (none today)
        for c in _connectors:
            try:
                items = await c.poll()
                assistant_store.sync_source(c.name, items)
            except Exception:  # noqa: BLE001 — a connector must never break the loop
                pass
        # fire any due reminders
        try:
            for note in due_reminders(assistant_store):
                await hub.publish(note)
        except Exception:  # noqa: BLE001
            pass
        # once-daily brief at the configured hour (8am or 8pm), or first tick
        # after if Wisp wasn't running at that moment.
        try:
            await _maybe_daily_brief()
        except Exception:  # noqa: BLE001
            pass
        try:
            await _maybe_daily_profile()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(TICK_S)


_PROFILE_HOUR = 4          # 4am local — see _maybe_daily_profile
_last_profile: object = None

# One source per night, not all four — see _maybe_daily_profile. Ordered
# biggest-first (messages/email dominate build time; notes/calendar are
# single-batch and finish in seconds regardless of position), though the
# order mostly just decides which source is "tonight" on any given date.
_PROFILE_SOURCE_CYCLE = ["messages", "email", "notes", "calendar"]


async def _maybe_daily_profile() -> None:
    """Refresh ONE SOURCE of the user's profile each night, overnight,
    rotating through _PROFILE_SOURCE_CYCLE so the full profile is current
    again every 4 nights.

    Used to run all sources every night in one pass — 70+ sequential gpt-oss
    calls, 20+ minutes, holding the model exclusively the whole time. Even
    overnight (when a stalled chat/summary doesn't matter) that's a long
    sustained load, which is real fan/thermal cost on a laptop, not just a
    memory-budget one — a single night's chunk (one source, and mostly on the
    much lighter profile_map model — see models.yaml) is a fraction of that.
    The manual "Build My Profile" button and the `build_profile` agent tool
    are UNCHANGED — sources=None still means a full, all-at-once build, since
    that's an explicit user action and they're choosing to wait for it.

    Deliberately NOT at the same hour as the daily brief; 4am is after the
    day's mail/messages have synced and long before the 8am brief needs the
    fast model back.

    Skipped entirely if a build already ran within the last 20 hours — the
    manual "Build My Profile" button shares the same timestamp, so using Wisp
    normally never triggers a redundant overnight rebuild.
    """
    global _last_profile
    now = datetime.now()
    if not (_PROFILE_HOUR <= now.hour < _PROFILE_HOUR + 3):
        return
    if _last_profile == now.date():
        return
    from service.memory.profile import get_profile_meta
    last = (get_profile_meta() or {}).get("updated_at") or 0
    if last and (time.time() - last) < 20 * 3600:
        _last_profile = now.date()
        return
    _last_profile = now.date()      # set first so a failure can't retry-loop
    from service.memory.profile import build_profile
    source = _PROFILE_SOURCE_CYCLE[now.toordinal() % len(_PROFILE_SOURCE_CYCLE)]
    await build_profile(sources=[source])


async def _maybe_daily_brief() -> None:
    global _last_brief
    from service.config import get_daily_summary_hour
    hour = get_daily_summary_hour()
    now = datetime.now()
    # Fire once per day, only within a 4-hour window after the configured hour,
    # so opening Wisp at 3pm doesn't retroactively fire the 8am brief.
    if hour <= now.hour < hour + 4 and _last_brief != now.date():
        _last_brief = now.date()   # set first so a failure doesn't retry-loop
        from service.assistant.brief import run_scheduled_brief
        await run_scheduled_brief("morning" if hour < 12 else "evening")
