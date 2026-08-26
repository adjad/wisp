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

from service import idle
from service.assistant.store import assistant_store
from service.assistant.reminders import due_reminders
from service.assistant.hub import hub

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
        # This runs REAL model work — the brief is several generations. With
        # one resident model that does not interleave with a user's turn, it
        # contends with it: measured 2026-08-09, the same summarize call took
        # 12.8s alone and 319.7s while a background job was running, with
        # oMLX logging prefill throttling and LRU eviction throughout. Skip
        # this tick if the user is waiting; it's periodic, so the next tick
        # picks the work up.
        #
        # _fire_scheduled_sends below is deliberately NOT gated: it does no
        # model work, and a send the user scheduled for 6pm should go at 6pm
        # whether or not they happen to be typing.
        if not idle.foreground_busy():
            try:
                await _maybe_daily_brief()
            except Exception:  # noqa: BLE001
                pass
        try:
            await _fire_scheduled_sends()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(TICK_S)


async def _fire_scheduled_sends() -> None:
    """Deliver sends the user queued for now, and retire ones that came due
    while Wisp wasn't running.

    The user already approved each of these at scheduling time — see
    outbound_queue's docstring for why the confirmation happens there and not
    here. This function therefore sends WITHOUT prompting, which is the whole
    point, and is also why the staleness sweep matters: an unattended send is
    only acceptable when it goes out roughly when promised.
    """
    from service.assistant.outbound_queue import outbound_queue
    from service.assistant.outbox import request as app_request

    for row in outbound_queue.sweep_stale():
        # Tell the user rather than silently dropping it. A queued message that
        # never went and never said so is the worst outcome here — they think
        # it was delivered.
        await hub.publish({
            "type": "scheduled_send_missed",
            "channel": row["channel"], "display": row["display"],
            "body": row["body"], "when_ts": row["when_ts"],
        })

    for row in outbound_queue.due():
        if row["channel"] == "email":
            res = await app_request("send_email", {
                "to": [row["recipient"]], "cc": [],
                "subject": row["subject"] or "", "body": row["body"],
            })
        else:
            res = await app_request("send_message",
                                    {"to": row["recipient"], "text": row["body"]})
        ok = bool(res.get("ok"))
        outbound_queue.mark(row["id"], "sent" if ok else "failed",
                            "" if ok else str(res.get("error") or "unknown error"))
        await hub.publish({
            "type": "scheduled_send_result",
            "ok": ok, "channel": row["channel"], "display": row["display"],
            "error": "" if ok else str(res.get("error") or "unknown error"),
        })


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
