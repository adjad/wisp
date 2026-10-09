"""Wiring for the attention runner: the only attention code that touches live state.

The decisions live in `service/attention/` and are tested with fakes. This module supplies
the real sources (the Messages feed, the commitments store) and the real effects (the
existing verified reminder path and the event hub), and nothing more.

Nothing acts unless the user has set the mode to "live" (`/assistant/attention/settings`).
The default is "shadow": decisions are recorded in the ledger and nothing else happens.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from service.assistant.hub import hub
from service.assistant.store import assistant_store
from service.attention.detectors import local_timezone
from service.attention.ledger import Ledger
from service.attention.live import Plan, load_settings, local_midnight, settings_path
from service.attention.runner import process
from service.paths import MOE_DIR

MIN_INTERVAL_S = 120                # the scheduler ticks every 30s; parsing the feed is not free
FINGERPRINT_BUCKET_S = 900          # also recompute every 15 minutes: "soon" moves with the clock
ON_FILE_DAYS = 4                    # how far ahead the "already on the calendar?" check looks

_ledger: Ledger | None = None
_last_fingerprint: tuple | None = None
_last_run: float = 0.0
_task: "asyncio.Task | None" = None


def get_ledger() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger(MOE_DIR / "attention" / "ledger.db")
    return _ledger


def reset() -> None:
    """For tests: forget the cached ledger and fingerprint."""
    global _ledger, _last_fingerprint, _last_run, _task
    if _task is not None and not _task.done():
        _task.cancel()
    _ledger, _last_fingerprint, _last_run, _task = None, None, 0.0, None


def _commitments(now: float) -> list[dict]:
    # From local midnight, not from now: `upcoming` drops anything that started more than
    # five minutes ago, which would hide today's all-day events and make a message about
    # an exam that is already on the calendar look uncaptured.
    return assistant_store.upcoming(local_midnight(now, local_timezone()), days=ON_FILE_DAYS + 1)


async def create_reminder(plan: Plan) -> dict:
    """Create the reminder through the existing verified path (`add_reminder`).

    That tool already owns the hard parts: a deterministic action id (so an identical
    reminder cannot be created twice), a refusal to fall back when an earlier attempt's
    outcome is unknown, and a Wisp-only fallback when Apple Reminders declines. Its result
    is prose, so it is classified conservatively: only "Reminder set:" is success.
    """
    from service.tools.assistant_tools import add_reminder
    # With an explicit offset, add_reminder's parse is exact whatever the process's own TZ is,
    # and a time in a repeated DST hour keeps its meaning.
    when_iso = datetime.fromtimestamp(plan.due_ts, ZoneInfo(local_timezone())).isoformat(timespec="minutes")
    text = await add_reminder(plan.title, when_iso, "reminder")
    if text.startswith("Reminder set:"):
        where = "wisp_only" if "Wisp only" in text else "apple_and_wisp"
        return {"ok": True, "status": "succeeded", "where": where}
    lowered = text.lower()
    unknown = any(k in lowered for k in ("unconfirmed", "not verified", "nothing was confirmed"))
    return {"ok": False, "status": "unknown" if unknown else "failed", "error": text[:300]}


def _name_for(handle: str) -> str | None:
    """Contact name for a phone/email handle, from the Messages contact cache (best effort)."""
    try:
        from service.tools import imessage_tools as im
        key = im._norm_handle(handle)
        if not key:
            return None
        for name, handles in im._name_handles.items():
            if any(im._norm_handle(h) == key for h in handles):
                return name.title()
    except Exception:  # noqa: BLE001 — a name is a nicety, never a reason to fail
        pass
    return None


async def _publish(event: dict) -> None:
    # Transient on purpose. A durable event is stored (with the quote and the sender) and
    # replayed to the app until it acknowledges it, and the app does not yet know this
    # type, so it never would: the rows would pile up in assistant.db without expiry.
    await hub.publish(event, durable=False)


async def run_tick(now: float | None = None) -> list:
    """One scheduler pass. Cheap when nothing changed; never raises into the scheduler."""
    global _last_fingerprint, _last_run
    now = now if now is not None else time.time()
    if now - _last_run < MIN_INTERVAL_S:
        return []
    _last_run = now
    settings = load_settings(settings_path(MOE_DIR))
    if settings.mode == "off":
        get_ledger().set_meta("last_mode", "off")     # so resuming live re-baselines
        return []
    from service.tools import imessage_tools
    feed = imessage_tools.structured_messages_snapshot()
    # Only a read completed in THIS launch is trusted; restored rows are not "new".
    if feed.get("state") != "ready" or (feed.get("coverage") or {}).get("status") == "unavailable":
        return []
    records = feed.get("records") or []
    fingerprint = (settings, len(records), max((r["timestamp"] for r in records), default=0),
                   int(now // FINGERPRINT_BUCKET_S))
    if fingerprint == _last_fingerprint:
        return []
    outcomes = await process(
        records=records, commitments=_commitments(now), now=now, tz=local_timezone(),
        settings=settings, ledger=get_ledger(), create=create_reminder, publish=_publish,
        fresh_commitments=lambda: _commitments(time.time()), name_for=_name_for)
    if not getattr(outcomes, "limited", False):
        _last_fingerprint = fingerprint               # a limited pass has work left: look again soon
    return outcomes


def schedule_tick() -> None:
    """Start a pass in the background, at most one at a time.

    The scheduler loop also delivers the user's scheduled sends and reminders, so it must
    never wait on a reminder write (which can take 45 seconds to time out). A pass that is
    still running when the next tick arrives simply means this tick is skipped.
    """
    global _task
    if _task is not None and not _task.done():
        return

    async def _run() -> None:
        try:
            await run_tick()
        except Exception:  # noqa: BLE001 — a failed pass must never surface in the scheduler
            pass

    _task = asyncio.get_running_loop().create_task(_run())


async def undo(source_id: str) -> dict:
    """Remove exactly the reminder this runner created for `source_id`, and nothing else.

    Only a ledger row in state `created` qualifies, and the reminder must match its recorded
    title and time. The deletion itself is the existing verified path (`_retire`).
    """
    ledger = get_ledger()
    row = ledger.get(source_id)
    if not row or row["state"] != "created":
        return {"ok": False, "error": "Wisp did not create a reminder for that message."}
    due = float(row["due_ts"])
    matches = [c for c in assistant_store.upcoming(due - 120, days=2)
               if c.get("title") == row["title"] and c.get("when_ts") is not None
               and abs(float(c["when_ts"]) - due) <= 90 and c.get("source") in ("reminders", "manual")]
    if not matches:
        return {"ok": False, "error": "The reminder was not found. It may already be gone."}
    if len(matches) > 1:
        return {"ok": False, "error": "More than one reminder matches; nothing was removed."}
    from service.tools.assistant_tools import _retire
    error = await _retire(matches[0])
    if error:
        return {"ok": False, "error": error}
    ledger.finish(source_id, "undone", {**row["detail"], "undone": True})
    try:
        await hub.publish({"type": "changed"})
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}
