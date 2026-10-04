"""Current-launch readiness for the sources behind Wisp's assistant.

Persisted caches make a restart fast, but they are not evidence that the app
has read the source in *this* launch. This module turns source-specific signals
into the three states the UI and tools need: ``syncing``, ``ready``, and
``unavailable``.
"""
from __future__ import annotations

import asyncio
import time
from typing import Iterable

from service.assistant import scheduler

DAILY_SOURCE_IDS = ("calendar", "reminders", "email", "messages")
SOURCE_IDS = (*DAILY_SOURCE_IDS, "notes", "browser_history")
_LABELS = {"calendar": "Calendar", "reminders": "Reminders",
           "email": "Email", "messages": "Messages", "notes": "Notes",
           "browser_history": "Browser History"}


# How long a native source (Calendar, Reminders) may stay "syncing" before it is
# reported as unavailable. A real first read takes seconds. A wait that outlasts
# this is not a sync in progress: macOS has not been given an answer to the
# permission prompt, or the app is not reporting at all.
SYNC_STALL_S = 90.0


def _stalled_reason(source: str, raw: dict) -> str:
    label = _LABELS[source]
    if not raw.get("last_sync"):
        return (f"The Wisp app hasn't reported {label} yet. Make sure Wisp is running, "
                "then try again.")
    return (f"{label} access hasn't been granted. Open System Settings > Privacy & Security > "
            f"{label} and turn Wisp on.")


def _scheduled_source(source: str) -> dict:
    raw = scheduler.connectors_status().get(source) or {}
    reason = str(raw.get("reason") or "")
    if raw.get("syncing") or not raw.get("last_sync"):
        since = raw.get("syncing_since") or scheduler.STARTED_AT
        if time.time() - since > SYNC_STALL_S:
            state, reason = "unavailable", _stalled_reason(source, raw)
        else:
            state = "syncing"
    else:
        state = "ready" if raw.get("available") else "unavailable"
    return {"id": source, "label": _LABELS[source], "state": state,
            "count": int(raw.get("count") or 0), "reason": reason}


def source_status(source: str) -> dict:
    if source in {"calendar", "reminders"}:
        return _scheduled_source(source)
    if source == "email":
        from service.tools import email_tools as email
        return {"id": source, "label": _LABELS[source],
                "state": email.email_sync_state(), "count": 0,
                "reason": email._email_reason,
                "read_source": email._email_read_source,
                "warning": email.email_freshness_warning()}
    if source == "messages":
        from service.tools.imessage_tools import messages_sync_state
        return {"id": source, "label": _LABELS[source],
                "state": messages_sync_state(), "count": 0, "reason": ""}
    if source == "notes":
        from service.tools.notes_tools import notes_sync_state
        return {"id": source, "label": _LABELS[source],
                "state": notes_sync_state(), "count": 0, "reason": ""}
    if source == "browser_history":
        from service.tools.browser_history_tools import browser_history_sync_state
        return {"id": source, "label": _LABELS[source],
                "state": browser_history_sync_state(), "count": 0, "reason": ""}
    raise KeyError(source)


def sources_snapshot(source_ids: Iterable[str] = DAILY_SOURCE_IDS) -> dict:
    sources = [source_status(source) for source in dict.fromkeys(source_ids)]
    for source in sources:
        # Most native readers expose completion, not a total-record count or
        # download progress. Report confirmed completion, never a timer-based
        # estimate. Failed/disabled reads must not look 100% successfully synced.
        state = source["state"]
        source["progress"] = 1.0 if state == "ready" else 0.0 if state == "syncing" else None
        source["progress_detail"] = (
            "Current data received" if state == "ready" else
            "Waiting for current data; intermediate progress unavailable" if state == "syncing" else
            "Turned off" if state == "disabled" else
            (source.get("reason") or "Could not read this source"))
        if source.get("warning"):
            source["progress_detail"] = "Local Mail read complete; server freshness unverified"
        elif source["id"] == "email" and state == "ready":
            source["progress_detail"] = "Mail data read"
        if source["id"] == "browser_history" and state == "syncing":
            from service.tools.browser_history_tools import _BROWSERS, _completed
            completed = len(_completed.intersection(_BROWSERS))
            source["progress"] = completed / len(_BROWSERS)
            source["progress_detail"] = f"{completed} of {len(_BROWSERS)} browser checks finished"
    pending = [source for source in sources if source["state"] == "syncing"]
    terminal = len(sources) - len(pending)
    return {
        "sources": sources,
        # Unavailable is terminal: the bar finishes, and the brief explicitly
        # degrades that source instead of spinning forever.
        "progress": terminal / len(sources) if sources else 1.0,
        "completed": terminal,
        "total": len(sources),
        "pending": [source["id"] for source in pending],
        "pending_labels": [source["label"] for source in pending],
        "syncing": bool(pending),
    }


def summary_snapshot() -> dict:
    return sources_snapshot(DAILY_SOURCE_IDS)


async def ensure_sources(sources: Iterable[str], timeout_seconds: float = 2.5) -> dict:
    """Ask the Swift app for current reads, then briefly await its POSTs."""
    wanted = tuple(dict.fromkeys(sources))
    if not wanted:
        return {"sources": [], "syncing": False}
    # A user retry after opening Mail must re-read it, not reuse the completed
    # local-only snapshot until the next five-minute timer. Completion of this
    # retry requires a NEW header push; the existing ready flag is not enough.
    from service.tools import email_tools as email
    refresh_local = "email" in wanted and bool(email.email_freshness_warning())
    email_generation = email._headers_sync_generation
    # A ready flag describes the previous native snapshot, not the state of
    # Reminders.app when this user query began. Require a new fetch generation.
    refresh_reminders = "reminders" in wanted
    # Assumes the service and the Swift app share one host clock: the app's
    # snapshot_started_at is compared with this time.time() request stamp.
    requested_at = time.time() if refresh_reminders else 0.0
    if not refresh_local and not refresh_reminders and all(
            source_status(source)["state"] != "syncing" for source in wanted):
        states = [source_status(source) for source in wanted]
        return {"sources": states, "syncing": False}
    try:
        from service.assistant.hub import hub
        await hub.publish({"type": "sync_assistant_sources_now",
                           "sources": list(wanted)})
    except Exception:  # noqa: BLE001 — readiness still reports the truth
        pass
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        states = [source_status(source) for source in wanted]
        email_refreshed = (not refresh_local or
                           email._headers_sync_generation > email_generation or
                           email.email_sync_state() == "unavailable")
        native = scheduler.connectors_status().get("reminders") or {}
        reminder_started = (native.get("diagnostics") or {}).get("snapshot_started_at")
        reminders_refreshed = (not refresh_reminders or
                               isinstance(reminder_started, (int, float)) and
                               reminder_started >= requested_at)
        if email_refreshed and reminders_refreshed and all(
                state["state"] != "syncing" for state in states):
            return {"sources": states, "syncing": False,
                    "reminders_fresh": reminders_refreshed}
        await asyncio.sleep(0.2)
    states = [source_status(source) for source in wanted]
    native = scheduler.connectors_status().get("reminders") or {}
    reminder_started = (native.get("diagnostics") or {}).get("snapshot_started_at")
    reminders_refreshed = (not refresh_reminders or
                           isinstance(reminder_started, (int, float)) and
                           reminder_started >= requested_at)
    return {"sources": states,
            "syncing": any(state["state"] == "syncing" for state in states)
                       or not reminders_refreshed,
            "reminders_fresh": reminders_refreshed}


# A real Notes read walks up to ~500 notes through AppleScript or the local
# store; a lookup is worth waiting for that, but not indefinitely.
NOTES_REFRESH_TIMEOUT_S = 8.0


async def refresh_notes(timeout_seconds: float | None = None) -> dict:
    """Ask the app for a NEW Notes read and report honestly what came back.

    ``status`` is one of
      ``fresh``        a read that STARTED after this request has been published;
      ``stale``        no such read arrived in time; a snapshot of ``age_seconds``
                       is the best we hold (``timed_out`` is True);
      ``timeout``      no such read arrived in time and we hold no snapshot at all;
      ``unavailable``  the app is not connected, or reported that Notes cannot be
                       read (``reason`` says why; a held snapshot's age is still
                       given in ``age_seconds``).

    The read that was already in flight when this request arrived finishes with
    an older view of Notes. Its publish carries an older ``snapshot_started_at``
    and is therefore never accepted as fresh; neither is a publish that carries
    no start stamp at all, since it cannot be told apart from that case.
    """
    from service.tools import notes_tools as notes
    # Assumes the service and the Swift app share one host clock.
    requested_at = time.time()

    def result(status: str, reason: str = "") -> dict:
        age = notes.snapshot_age_seconds()
        return {"status": status, "requested_at": requested_at, "reason": reason,
                "age_seconds": age, "timed_out": status in {"stale", "timeout"},
                "fresh": status == "fresh"}

    try:
        from service.assistant.hub import hub
        if not hub.has_subscribers:
            return result("unavailable", "The Wisp app is not connected, so Notes could "
                                         "not be re-read")
        await hub.publish({"type": "sync_assistant_sources_now", "sources": ["notes"]},
                          durable=False)
    except Exception:  # noqa: BLE001 — report the truth instead of guessing
        return result("unavailable", "Wisp could not ask the app to re-read Notes")
    if timeout_seconds is None:
        timeout_seconds = NOTES_REFRESH_TIMEOUT_S
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        receipt = notes.notes_receipt()
        if receipt["started_at"] and receipt["started_at"] >= requested_at:
            if receipt["available"]:
                return result("fresh")
            return result("unavailable", receipt["reason"] or "Notes could not be read")
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    if notes.snapshot_age_seconds() is None:
        return result("timeout", "Notes did not answer in time")
    return result("stale", "Notes did not answer in time")


async def ensure_daily_sources(timeout_seconds: float = 8.0) -> dict:
    readiness = await ensure_sources(DAILY_SOURCE_IDS, timeout_seconds=timeout_seconds)
    snapshot = summary_snapshot()
    if readiness.get("reminders_fresh", True):
        return snapshot
    # The connector's old ready flag must not turn a timed-out user refresh
    # into a successful Daily Summary of cached reminder rows.
    for source in snapshot["sources"]:
        if source["id"] == "reminders":
            source.update(state="syncing", progress=0.0,
                          reason="Waiting for a current Reminders read",
                          progress_detail="Waiting for current Reminders data")
    snapshot["pending"] = [source["id"] for source in snapshot["sources"]
                           if source["state"] == "syncing"]
    snapshot["pending_labels"] = [source["label"] for source in snapshot["sources"]
                                  if source["state"] == "syncing"]
    snapshot["completed"] = snapshot["total"] - len(snapshot["pending"])
    snapshot["progress"] = snapshot["completed"] / snapshot["total"]
    snapshot["syncing"] = True
    return snapshot


def _natural_join(names: list[str]) -> str:
    names = [name.lower() for name in names]
    if len(names) <= 1:
        return names[0] if names else "data"
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def daily_syncing_message(snapshot: dict | None = None) -> str:
    snapshot = snapshot or summary_snapshot()
    names = list(snapshot.get("pending_labels") or [])
    return (f"Wisp is still syncing your {_natural_join(names)} after launch, "
            "so I’m holding off on the daily summary rather than showing you "
            "an incomplete one. The sync status will update as each source "
            "finishes; try again in a moment.")
