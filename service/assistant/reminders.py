"""Reminder engine — pure lead-time rules over the commitments store.

For each active future commitment it decides which "stages" (e.g. 1 day before,
30 min before) are due and haven't fired yet, deduping through notify_log so a
restart replays only unacknowledged events. When several stages for one commitment have already
passed (e.g. you just added a same-day event), it fires only the most imminent
and supersedes earlier pending stages — no burst of stale alerts.
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


def due_reminders(store, now: float | None = None) -> list[dict]:
    """Persist due notifications; stages become final only on app acknowledgement."""
    now = now if now is not None else time.time()
    out: list[dict] = []
    for c in store.reminder_snapshot(now):
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
        passed.sort(key=lambda x: x[1])          # smallest lead = most imminent
        target_label, _ = passed[0]
        if store.already_notified(c["id"], target_label):
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
        }
        row = store.enqueue_event(payload, dedupe_key=key, target={
            "type": "reminder", "identity": [" ".join(c["title"].split()).casefold(), int(when // 60)],
            "schedules": {cid: member["when_ts"] for cid, member in snapshot.items()},
            "generations": {cid: member["generation"] for cid, member in snapshot.items()},
            "when_ts": when, "stages": [label for label, _ in passed],
        }, reminder_snapshot=snapshot)
        if row is not None:
            out.append({**row["payload"], "event_id": row["id"]})
    return out
