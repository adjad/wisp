"""Evidence semantics shared by mining, reports and legacy stored patterns.

A calendar entry describes a plan. A checked reminder describes a recorded
completion, whose date is known only when the native source actually supplies it.
User confirmation of a *pattern* never confirms attendance at individual events.
"""

from __future__ import annotations

import math
from dataclasses import replace

from service.behavior.types import Observation, Pattern


def completion_time(o: Observation) -> float | None:
    value = o.attrs.get("completion_ts")
    if (
        o.kind != "reminder_done"
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        return None
    return float(value) if math.isfinite(value) else None


def basis(o: Observation) -> str:
    if o.kind == "event":
        return "scheduled"
    if o.kind == "reminder_done":
        return "recorded_completion"
    if o.kind in {"event_cancelled", "event_moved"}:
        return "inferred_schedule_change"
    if o.kind in {"message_in", "message_out", "wisp_ask", "note"}:
        return "observed"
    return "unknown"


def _contact_source_counts(p: Pattern | None) -> tuple[bool, bool]:
    values = (p.stats.get("calls"), p.stats.get("messages")) if p else (None, None)
    return tuple(type(value) is int and value > 0 for value in values)


def pattern_evidence(p: Pattern) -> Pattern:
    """Normalize old saved/confirmed statements on read without changing verdicts.

    The original statement remains available for traceability. In particular an
    orphan confirmed v1 habit must not bypass the new semantics before re-mining.
    """
    if p.kind == "contact_rhythm":
        calls, messages = _contact_source_counts(p)
        stats = dict(p.stats)
        # Invalid legacy counts are unknown, including non-finite values that
        # cannot be emitted by the JSON API. Preserve the original wording only.
        for key in ("calls", "messages"):
            value = stats.get(key)
            stats[key] = value if type(value) is int and value >= 0 else None
        stats["basis"] = "mixed" if calls and messages else "scheduled" if calls else "observed" if messages else "unknown"
        if calls or messages:
            from service.behavior.timeutil import when_phrase
            part = stats.get("daypart")
            when = (when_phrase(p.window.weekdays, part, most=len(p.window.weekdays) != 1)
                    if isinstance(part, str) and part in {"morning", "afternoon", "evening", "night"} else "in this window")
            source = "Messages and scheduled calls" if calls and messages else "Scheduled calls" if calls else "Recorded messages"
            statement = f"{source} with {p.subject} recur {when} ({_range(p)})"
            if calls:
                statement += "; attendance unknown"
        else:
            statement = f"Saved contact window with {p.subject} ({_range(p)}); recorded message/call source counts unavailable"
        stats.update(evidence_version=2, original_statement=stats.get("original_statement", p.statement))
        return replace(p, statement=statement, stats=stats)
    if p.stats.get("evidence_version") == 2:
        return p
    stats = dict(p.stats)
    statement = p.statement
    if p.kind == "habit":
        kt = stats.get("key_type", "subject")
        if kt in {"category", "subject"}:
            stats["basis"] = "scheduled"
            if not statement.startswith(("Your calendar", "Calendar:")):
                statement = "Calendar: " + statement.replace(
                    "You usually ", "Usually scheduled to ", 1
                ).replace("You ", "Scheduled to ", 1)
                statement += "; completion unknown"
        else:
            stats["basis"] = "recorded_completion" if kt == "reminder" else "observed"
    elif p.kind == "habit_protection":
        stats["basis"] = "inferred_schedule_change"
        counts = [stats.get(k) for k in ("collisions", "moved", "cancelled", "kept")]
        if (all(type(v) is int and v >= 0 for v in counts)
                and counts[0] > 0 and sum(counts[1:]) == counts[0]):
            n, moved, removed, kept = counts
            statement = (f"Calendar overlap pattern for {p.subject}: {moved} moved, {removed} removed, "
                         f"{kept} remained scheduled out of {n} overlapping bookings; attendance unknown")
        else:
            statement = (f"Calendar overlap pattern for {p.subject}; historical change counts unavailable; "
                         "attendance unknown")
    elif p.kind == "cancel_propensity":
        from service.behavior.miners import bucket_phrase
        stats["basis"] = "inferred_schedule_change"
        count, total = p.support, p.opportunities
        action = stats.get("action")
        if (type(count) is int and type(total) is int and 0 <= count <= total
                and total > 0 and action in {"move", "cancel"}):
            change = "moved" if action == "move" else "removed before their start"
            bucket = bucket_phrase(str(stats.get("bucket") or p.subject))
            statement = (f"Calendar records: {count} of the last {total} {bucket} {change}; "
                         "attendance unknown")
        else:
            statement = (f"Calendar change pattern for {p.subject}; historical change counts unavailable; "
                         "attendance unknown")
    elif p.kind == "day_shape":
        stats["basis"] = "scheduled"
        # Keep the measured association, but avoid attributing an inferred edit
        # to the user's attendance or intention.
        statement = "Calendar pattern: " + statement.removeprefix("You ").replace(
            "you have ", "there are "
        ).replace("you cancel ", "events are removed or moved ").replace(
            "you move it", "the overlapping event moves"
        ).replace(
            "lunch is usually free", "the midday calendar gap is usually"
        )
    else:
        stats["basis"] = "observed"
    stats.update(evidence_version=2, original_statement=p.statement)
    return replace(p, statement=statement, stats=stats)


def _range(p: Pattern) -> str:
    from service.behavior.timeutil import fmt_range

    return fmt_range(p.window.start_minute, p.window.end_minute)
