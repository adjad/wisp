"""When an undated Apple reminder has gone stale, and what Wisp does about it.

The user's rule (2026-10-18): an undated reminder about a week old gets one question,
"keep it or delete it?"; anything older that they have not chosen to keep is neither
mentioned nor used. The same rule covers dated reminders a week or more overdue.

Ages count from EventKit's creationDate (``created_ts``). A reminder whose creation date
is unknown is treated as unknown age, never as stale, so nothing disappears by guesswork.

States:
- ``fresh``   younger than ASK_AFTER_S; used normally.
- ``unknown`` no creation date; used normally.
- ``kept``    the user chose Keep; used normally and never asked about again.
- ``ask``     ASK_AFTER_S <= age < ASK_UNTIL_S and not kept; Wisp offers Keep / Delete
              once (the notch shows it until the user answers), and nothing else uses it.
- ``stale``   older than ASK_UNTIL_S and not kept; hidden everywhere, no question either.

Dated reminders follow the same states, aged by how long they are OVERDUE
(``classify_overdue``); one not yet a week past due is untouched.

Reading of "anything older than that": from one week on, an unkept undated reminder is
already excluded from briefs, Day plans, attention, alerts and model context; the only
place it appears is the question itself, offered for one more week. After that it is
silent. Reminders.app is never changed except by an explicit Delete click.
"""
from __future__ import annotations

import math

DAY_S = 86400
ASK_AFTER_S = 7 * DAY_S
ASK_UNTIL_S = 14 * DAY_S

KEEP = "keep"
EXCLUDED_STATES = frozenset({"ask", "stale"})


def valid_created_ts(value: object) -> float | None:
    """A usable creation time, or None. Booleans, non-finite and non-positive values are
    not times; a future time (clock skew) is still accepted and simply reads as fresh."""
    if type(value) not in (int, float):
        return None
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value >= 253402300800:
        return None
    return value


def classify(created_ts: object, decision: str | None, now: float) -> str:
    if decision == KEEP:
        return "kept"
    created = valid_created_ts(created_ts)
    if created is None:
        return "unknown"
    age = now - created
    if age < ASK_AFTER_S:
        return "fresh"
    if age < ASK_UNTIL_S:
        return "ask"
    return "stale"


def classify_overdue(due_ts: object, decision: str | None, now: float) -> str:
    """The same states for a DATED incomplete Apple reminder, aged by time past its due
    time (2026-10-18: "Get doordash order", due Oct 2, still showed 16 days later). Not
    yet overdue, or overdue by less than ASK_AFTER_S, reads ``fresh``."""
    if decision == KEEP:
        return "kept"
    due = valid_created_ts(due_ts)
    if due is None:
        return "unknown"
    overdue = now - due
    if overdue < ASK_AFTER_S:
        return "fresh"
    if overdue < ASK_UNTIL_S:
        return "ask"
    return "stale"


def excluded(created_ts: object, decision: str | None, now: float) -> bool:
    """True when this undated reminder must not be used or mentioned anywhere."""
    return classify(created_ts, decision, now) in EXCLUDED_STATES


def policy() -> dict:
    return {"ask_after_days": ASK_AFTER_S // DAY_S, "ask_until_days": ASK_UNTIL_S // DAY_S,
            "applies_to": "incomplete Apple reminders: undated ones by age, dated ones by time overdue",
            "age_from": {"undated": "Reminders creation date; unknown creation date is never stale",
                         "overdue": "due time"}}
