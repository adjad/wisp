"""What waits for quiet hours to end, and what no longer matters by then.

Quiet hours (service/user_hours.py) silence what asks for the person's attention:
notifications, sounds, notch pop-ups, check-ins. Wisp keeps working in the
background, and nothing is lost: these events stay pending in the assistant store,
and Hub.events delivers them on the first stream after quiet hours end (it rotates
every 25 seconds). One that has stopped being true by then is dropped instead.

Only what Wisp starts on its own is held. An Ask turn and its confirmation cards,
the outcome of an action the person took, a scheduled send, and a timer, reminder
or event alert they set for this time all arrive as usual.
"""
from __future__ import annotations

import time
from datetime import datetime

from service import user_hours

HELD_TYPES = frozenset({"nudge", "attention_added", "checkin", "daily_brief", "email_summary",
                        "node_result", "codex_task_update", "google_signin_expired"})
# A week's or a day's notice can wait until morning. Nearer stages are the alert the
# person set for this time ("T-3h", "T-30m", "T-10m", "due").
_LEAD_REMINDER_STAGES = frozenset({"T-1w", "T-1d"})
# Someone who just finished onboarding at night is waiting for the first Brief.
_ONBOARDING_KEY = "install:onboarding_answered_at"  # scheduler.ONBOARDING_ANSWERED_KEY
_ONBOARDING_WAITING_S = 2 * 3600


def _quiet_now() -> bool:
    return user_hours.in_quiet_hours()


def _waiting_for_first_brief(store, now: float) -> bool:
    try:
        answered = store.completion(_ONBOARDING_KEY)
        return answered is not None and 0 <= now - float(answered) <= _ONBOARDING_WAITING_S
    except Exception:                     # noqa: BLE001 - unknown means nobody is known to be waiting
        return False


def asks_for_attention(event: dict) -> bool:
    kind = event.get("type")
    if kind == "reminder":
        return event.get("stage") in _LEAD_REMINDER_STAGES
    if kind == "daily_brief" and event.get("recomposed"):
        return False                      # updates the Brief tab with no banner
    return kind in HELD_TYPES


def held(event: dict, store=None, *, now: float | None = None) -> bool:
    """Should this event wait? Only an event with a stored row can wait: a transient
    one has nothing to come back to."""
    if not event.get("event_id") or not asks_for_attention(event) or not _quiet_now():
        return False
    if event.get("type") == "daily_brief" and store is not None:
        return not _waiting_for_first_brief(store, time.time() if now is None else now)
    return True


def _same_local_day(timestamp, now: float) -> bool:
    return datetime.fromtimestamp(timestamp).date() == datetime.fromtimestamp(now).date()


def stale(event: dict, *, now: float | None = None) -> bool:
    """Has this event stopped being worth showing? Never true of anything not held,
    so a late delivery of anything else behaves as it always did."""
    if not asks_for_attention(event):
        return False
    now = time.time() if now is None else now
    kind = event.get("type")
    try:
        if kind == "checkin":             # "You planned 3 things today" is about that evening
            return event.get("day") != datetime.fromtimestamp(now).date().isoformat()
        if kind == "daily_brief":         # last night's brief, when the morning's is coming
            created = event.get("created_at")
            return isinstance(created, (int, float)) and not _same_local_day(created, now)
        if kind == "attention_added":     # the plan it reminds about has passed
            until = event.get("relevant_until")
            return isinstance(until, (int, float)) and until <= now
    except (OSError, OverflowError, ValueError):
        return False
    return False
