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
import json
import time
from datetime import date, datetime

from service import idle
from service.assistant.store import assistant_store
from service.assistant.reminders import due_reminders
from service.assistant.hub import hub

# The date the scheduled brief was last DELIVERED, cached from disk. It lives on
# disk (not just here) because the backend is a child process of Wisp.app: every
# relaunch used to reset this, so relaunching inside the schedule's window fired
# the brief again — and right after a launch the sources are always still
# syncing, so what fired was a hold-back message announced as a finished summary.
_last_brief: date | None = None
_last_brief_loaded = False
# Ticks spent waiting for the launch sync before giving the day up. At TICK_S=30
# this is ~10 minutes, which covers a slow multi-account Mail read without
# retrying a genuinely broken source every 30s until midnight.
_MAX_BRIEF_ATTEMPTS = 20
_brief_attempts: dict[date, int] = {}

TICK_S = 30.0

# Server-side connectors that DON'T need macOS TCC (none yet; mail lands here).
_connectors: list = []

# Last-sync bookkeeping for sources fed by the app, surfaced in /assistant/status.
_sync_status: dict[str, dict] = {
    "calendar": {"available": False, "reason": "waiting for the app to sync", "count": 0},
    "reminders": {"available": False, "reason": "waiting for the app to sync", "count": 0},
}


def record_sync(source: str, count: int, diagnostics: dict | None = None) -> None:
    diagnostics = diagnostics or {}
    # A completed EventKit read with authorized=false is unavailable, not a
    # successful empty calendar/reminder list.
    syncing = bool(diagnostics.get("syncing"))
    available = (not syncing and diagnostics.get("authorized") is not False
                 and diagnostics.get("available") is not False)
    reason = str(diagnostics.get("reason") or (
        "waiting for the app to sync" if syncing else "ok" if available
        else f"{source.title()} could not be read; check access in Settings"))
    _sync_status[source] = {
        "available": available, "syncing": syncing, "reason": reason, "count": count,
        "last_sync": time.time(), "diagnostics": diagnostics,
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
        # The brief itself is now a deterministic render (no generation — see
        # brief._generate_brief), but composing it still asks the Swift app for a
        # current read of Calendar, Reminders, Mail and Messages, and those reads
        # are what a user's own mail/message question is waiting on too. Skip this
        # tick if the user is waiting; it's periodic, so the next tick picks the
        # work up.
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
        # Codex task monitoring is a read-only local poll. It establishes a
        # baseline on first use, then publishes only terminal transitions or a
        # one-time stalled alert; ordinary progress commentary stays quiet.
        try:
            from service.codex_monitor import codex_monitor
            for event in codex_monitor.poll_events():
                await hub.publish(event)
        except Exception:  # noqa: BLE001 — Codex may not be installed/open yet
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

    # Never silently retry a send whose outcome we lost. Move it out of
    # `sending` once, then keep offering the notice until something is actually
    # connected to receive it — otherwise a recovery that happens while the app
    # is closed tells nobody, ever.
    outbound_queue.recover_in_flight()
    for row in outbound_queue.unacknowledged_unknown():
        # Republished every sweep until the app acknowledges it. `notice_id` is
        # stable across replays so the app can drop the duplicates; publishing
        # is not itself evidence that anyone received the notice.
        await hub.publish({
            "type": "scheduled_send_unknown",
            "notice_id": row["id"],
            "channel": row["channel"], "display": row["display"],
            "body": row["body"], "when_ts": row["when_ts"],
        })

    for row in outbound_queue.due():
        if not outbound_queue.claim(row["id"]):
            continue
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


def _brief_state_path():
    from service.paths import MOE_DIR
    return MOE_DIR / "brief_state.json"


def _brief_date() -> date | None:
    """The date the brief last went out, read from disk once per process."""
    global _last_brief, _last_brief_loaded
    if not _last_brief_loaded:
        _last_brief_loaded = True
        try:
            with _brief_state_path().open() as f:
                _last_brief = date.fromisoformat(json.load(f)["last_brief"])
        except Exception:  # noqa: BLE001 — absent or corrupt is simply "never"
            _last_brief = None
    return _last_brief


def _record_brief_date(day: date) -> None:
    global _last_brief
    _last_brief = day
    try:
        path = _brief_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump({"last_brief": day.isoformat()}, f)
    except Exception:  # noqa: BLE001 — an unwritable state file must not stop the brief
        pass


async def _maybe_daily_brief() -> None:
    from service.config import get_daily_summary_hour
    hour = get_daily_summary_hour()
    now = datetime.now()
    # Fire once per day, only within a 4-hour window after the configured hour,
    # so opening Wisp at 3pm doesn't retroactively fire the 8am brief.
    if not (hour <= now.hour < hour + 4) or _brief_date() == now.date():
        return
    attempts = _brief_attempts.get(now.date(), 0) + 1
    _brief_attempts[now.date()] = attempts
    from service.assistant.brief import run_scheduled_brief
    # Marked done only on real delivery. run_scheduled_brief returns False (and
    # publishes nothing) while the launch sync is still running, and burning the
    # day on that is how a morning ended up with a "summary is ready" ping and no
    # summary. The attempt cap keeps a permanently unreadable source from
    # retrying every tick — and, like the old set-before-await, stops a raise
    # here from looping.
    if await run_scheduled_brief("morning" if hour < 12 else "evening"):
        _record_brief_date(now.date())
    elif attempts >= _MAX_BRIEF_ATTEMPTS:
        _record_brief_date(now.date())
