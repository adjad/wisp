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


def pattern_evidence(p: Pattern) -> Pattern:
    """Normalize old saved/confirmed statements on read without changing verdicts.

    The original statement remains available for traceability. In particular an
    orphan confirmed v1 habit must not bypass the new semantics before re-mining.
    """
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
    elif p.kind in {"day_shape", "habit_protection", "cancel_propensity"}:
        stats["basis"] = (
            "scheduled" if p.kind == "day_shape" else "inferred_schedule_change"
        )
        # Keep the measured association, but avoid attributing an inferred edit
        # to the user's attendance or intention.
        statement = "Calendar pattern: " + statement.removeprefix("You ").replace(
            "you have ", "there are "
        ).replace("you cancel ", "events are removed or moved ").replace(
            "you move it", "the overlapping event moves"
        ).replace(
            "lunch is usually free", "the midday calendar gap is usually"
        )
    elif p.kind == "contact_rhythm":
        stats["basis"] = "mixed" if stats.get("calls", 0) else "observed"
        if stats["basis"] == "mixed" and not statement.startswith(
            "Messages and scheduled calls"
        ):
            statement = f"Message timing and scheduled calls suggest a recurring window with {p.subject} ({_range(p)})"
    else:
        stats["basis"] = "observed"
    stats.update(evidence_version=2, original_statement=p.statement)
    return replace(p, statement=statement, stats=stats)


def _range(p: Pattern) -> str:
    from service.behavior.timeutil import fmt_range

    return fmt_range(p.window.start_minute, p.window.end_minute)
