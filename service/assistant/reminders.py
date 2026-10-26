"""Reminder engine — pure lead-time rules over the commitments store.

For each active future commitment it decides which "stages" (e.g. 1 day before,
30 min before) are due and haven't fired yet, deduping through notify_log so a
restart replays only unacknowledged events. When several stages for one commitment have already
passed (e.g. you just added a same-day event), it fires only the most imminent
and supersedes earlier pending stages — no burst of stale alerts.

Every reminder event expires REMINDER_EXPIRY_S after its event time. An app that
reconnects later (sleep, quit, a lost stream) must not flush a stale "in 9 min"
banner for something that already happened: the store supersedes expired rows
before replay, and the app skips any it still receives.

Some rows never get Wisp's own alert, because something else already said it:
- Items Wisp added from a learned routine (behavior_runtime). Their nudge was
  the notification; a hold or a reminder due at the nudge would repeat it, and
  a "Tentative: … (Wisp suggested)" hold is not a plan to be warned about.
- A dated reminder Wisp itself wrote to Apple Reminders with an absolute alarm
  at its due time. macOS shows that alarm, so Wisp skips its own "due" stage.
"""
from __future__ import annotations

import time

# stage label -> seconds before `when_ts` it should fire, per kind.
_STAGES: dict[str, list[tuple[str, float]]] = {
    "exam":       [("T-1w", 7 * 86400), ("T-1d", 86400), ("T-3h", 3 * 3600)],
    "assignment": [("T-1d", 86400), ("T-3h", 3 * 3600)],
    "meeting":    [("T-30m", 1800), ("T-10m", 600)],
    "event":      [("T-30m", 1800), ("T-10m", 600)],
    "reminder":   [("due", 0)],
}

# grace after `when_ts` during which a just-passed trigger may still fire (s)
_GRACE = 60.0

# A reminder event is stale this long after its event time (s). Shared with the
# store, which applies it to rows queued before events carried expires_at.
REMINDER_EXPIRY_S = 120.0

# A routine reminder due this soon after Wisp created it is the nudge itself.
_BEHAVIOR_SAME_AS_NUDGE_S = 600.0


def _from_routine(c: dict) -> bool:
    """A row Wisp added from a learned routine whose nudge already notified."""
    from service.assistant.store import MANUAL_SOURCE
    if c.get("source") != MANUAL_SOURCE:
        return False
    from service.assistant.behavior_runtime import BEHAVIOR_CONTEXT
    routine = str(c.get("context") or "").startswith(BEHAVIOR_CONTEXT)
    if c.get("kind") != "reminder":
        # A "Tentative: … (Wisp suggested)" hold is a suggestion on Wisp's own
        # schedule, never a meeting to warn about.
        return routine or " ".join(str(c.get("title") or "").split()).endswith("(Wisp suggested)")
    if not routine:
        return False
    created = c.get("created_at")
    # A reminder the user asked for later in the day still fires at its time.
    return not isinstance(created, (int, float)) or c["when_ts"] - created < _BEHAVIOR_SAME_AS_NUDGE_S


def _native_alarm_covers(store, c: dict) -> bool:
    """Wisp wrote this reminder (or its twin) to Apple Reminders with an alarm at this time."""
    ids = [c["id"]] + list(c.get("duplicate_ids") or [])
    for cid in ids:
        row = c if cid == c["id"] else store.get(cid)
        if (row and row.get("source") == "reminders" and row.get("source_id")
                and store.native_alarm_covers(row["source_id"], c["when_ts"])):
            return True
    return False


def _humanize(delta_s: float) -> str:
    if delta_s <= 0:
        return "now"
    m = int(delta_s // 60)
    if m < 60:
        return f"in {m} min"
    h = m // 60
    if h < 24:
        rem = m % 60
        return f"in {h}h" + (f" {rem}m" if rem else "")
    d = h // 24
    return f"in {d} day" + ("s" if d != 1 else "")


def lapsed_stages(kind: str, when: float, now: float) -> list[str]:
    """Stages of a `kind` commitment at `when` whose trigger time is already past."""
    return [label for label, lead in _STAGES.get(kind, _STAGES["event"]) if when - lead <= now]


def due_reminders(store, now: float | None = None) -> list[dict]:
    """Persist due notifications; stages become final only on app acknowledgement."""
    now = now if now is not None else time.time()
    out: list[dict] = []
    for c in store.reminder_snapshot(now):
        if c.get('all_day'):
            continue  # A date-only due date supplies no notification clock.
        snapshot = c["reminder_snapshot"]
        when = c.get("when_ts")
        if when is None:
            continue
        stages = _STAGES.get(c["kind"], _STAGES["event"])
        # stages whose trigger time has passed but the event hasn't (plus grace)
        passed = [(label, lead) for label, lead in stages
                  if when - lead <= now < when + _GRACE]
        if not passed:
            continue
        if _from_routine(c):
            continue
        passed.sort(key=lambda x: x[1])          # smallest lead = most imminent
        target_label, _ = passed[0]
        if store.already_notified(c["id"], target_label):
            continue
        if target_label == "due" and _native_alarm_covers(store, c):
            continue
        # Stable across sync twins and restart, but a changed schedule gets a
        # new identity. Persist before returning anything to the scheduler.
        import hashlib
        identity = repr((" ".join(c["title"].split()).casefold(), int(when // 60), target_label,
                         snapshot[c["id"]]["generation"]))
        key = "reminder:" + hashlib.sha256(identity.encode()).hexdigest()
        if store.event_by_key(key):
            continue
        payload = {
            "type": "reminder",
            "commitment_id": c["id"],
            "kind": c["kind"],
            "title": c["title"],
            "context": c.get("context"),
            "when_ts": when,
            "stage": target_label,
            "when_label": _humanize(when - now),
            "expires_at": when + REMINDER_EXPIRY_S,
        }
        row = store.enqueue_event(payload, dedupe_key=key, target={
            "type": "reminder", "identity": [" ".join(c["title"].split()).casefold(), int(when // 60)],
            "schedules": {cid: member["when_ts"] for cid, member in snapshot.items()},
            "generations": {cid: member["generation"] for cid, member in snapshot.items()},
            "when_ts": when, "stages": [label for label, _ in passed],
        }, reminder_snapshot=snapshot, expires_at=when + REMINDER_EXPIRY_S)
        if row is not None:
            out.append({**row["payload"], "event_id": row["id"]})
    return out
