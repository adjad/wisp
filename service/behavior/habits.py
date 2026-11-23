"""Send-timing and reminder habits: deterministic counts over metadata, no text.

Inputs are observations that carry only times and flags:

* ``email_out``, ``message_out`` and ``note`` (source ``notes``): when the user
  sent an email or a message, or last edited a note;
* ``reminder_fact``: a reminder's creation, due and completion times and whether
  it repeats (never its title).

Every statement is composed from counts and medians. Nothing here reads or keeps
the content of a message, note or reminder.
"""
from __future__ import annotations

import statistics
from collections import Counter
from datetime import tzinfo
from typing import Iterable

from service.behavior.miners import pattern_id, wilson_lower
from service.behavior.timeutil import DAY_NAMES, daypart, fmt_range, local_dt, minute_of
from service.behavior.types import Observation, Pattern, Window

SEND_MIN_EVENTS = 20          # fewer sends than this say nothing about "usually"
SEND_MIN_DAYS = 14            # ...and they must span at least two weeks
SEND_MIN_SHARE = 0.4          # the busiest 3-hour window must hold this share of weekday sends
WINDOW_HOURS = 3
REMINDER_MIN = 10             # reminders needed before a reminder habit is stated
OVERDUE_LOOKBACK_S = 8 * 7 * 86400
SERIES_MIN_NOTES = 10

_SEND_NOUN = {"email": "sent emails", "message": "sent messages", "note": "notes last edited"}
_SEND_UNIT = {"email": "emails", "message": "messages", "note": "notes"}


def measure_confidence(n: int) -> float:
    """How far to trust a median or share taken over ``n`` items; 0.5 at n=20."""
    return round(n / (n + 20), 4) if n > 0 else 0.0


def _hour_label(start_hour: int, end_hour: int) -> str:
    return fmt_range(start_hour * 60, min(24, end_hour) * 60)


def _span(hours: float) -> str:
    h = abs(hours)
    if h < 1:
        return f"{max(1, round(h * 60))} minutes"
    if h < 36:
        n = round(h)
        return f"{n} hour{'s' if n != 1 else ''}"
    n = round(h / 24)
    return f"{n} days"


# ---------------------------------------------------------------- send timing
def mine_send_timing(channel: str, obs: Iterable[Observation], tz: tzinfo) -> Pattern | None:
    rows = sorted(obs, key=lambda o: o.ts)
    n = len(rows)
    if n < SEND_MIN_EVENTS or rows[-1].ts - rows[0].ts < SEND_MIN_DAYS * 86400:
        return None
    locals_ = [local_dt(o.ts, tz) for o in rows]
    weekday = [(dt.hour, dt.weekday()) for dt in locals_ if dt.weekday() < 5]
    if not weekday:
        return None
    hours = Counter(h for h, _ in weekday)
    best_start, best = max(((s, sum(hours[h] for h in range(s, s + WINDOW_HOURS)))
                            for s in range(0, 24 - WINDOW_HOURS + 1)), key=lambda kv: (kv[1], -kv[0]))
    share = best / len(weekday)
    if share < SEND_MIN_SHARE:
        return None
    days = Counter(dt.weekday() for dt in locals_)
    top_day, top_n = max(days.items(), key=lambda kv: (kv[1], -kv[0]))
    weekend = n - len(weekday)
    text = (f"Your {_SEND_NOUN[channel]} cluster on weekdays between "
            f"{_hour_label(best_start, best_start + WINDOW_HOURS)} "
            f"({round(100 * share)}% of {len(weekday)} on weekdays); "
            f"{DAY_NAMES[top_day]} is the busiest day ({round(100 * top_n / n)}%)")
    if weekend >= 5 and weekend / n >= 0.2:
        text += f"; {round(100 * weekend / n)}% are on weekends"
    window = Window(weekdays=(0, 1, 2, 3, 4), start_minute=best_start * 60,
                    end_minute=(best_start + WINDOW_HOURS) * 60)
    unit = _SEND_UNIT[channel]
    return Pattern(
        id=pattern_id("send_timing", channel), kind="send_timing", subject=channel, statement=text + ".",
        window=window, support=best, opportunities=len(weekday), confidence=wilson_lower(best, len(weekday)),
        last_seen=rows[-1].ts, evidence=tuple(o.id for o in rows[-5:]),
        stats={"channel": channel, "samples": n, "weekday_share": round(share, 3), "top_weekday": top_day,
               "top_weekday_pct": round(100 * top_n / n), "weekend_pct": round(100 * weekend / n),
               "daypart": daypart(best_start * 60 + 60),
               "evidence_label": f"{best} of {len(weekday)} weekday {unit} in that window",
               "basis": "observed", "evidence_version": 2})


# ---------------------------------------------------------------- reminders
def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
        and abs(value) != float("inf") else None


def _facts(obs: Iterable[Observation]) -> list[dict]:
    out = []
    for o in obs:
        a = o.attrs or {}
        out.append({"id": o.id, "created": _num(a.get("created_ts")), "due": _num(a.get("due_ts")),
                    "done": _num(a.get("completed_ts")), "repeating": a.get("repeating") is True})
    return out


def _pattern(subject: str, text: str, *, n: int, support: int, opportunities: int, label: str,
             last: float | None, ids: list[str], confidence: float | None = None, **stats) -> Pattern:
    return Pattern(
        id=pattern_id("reminder_habit", subject), kind="reminder_habit", subject=subject, statement=text,
        support=support, opportunities=opportunities,
        confidence=measure_confidence(n) if confidence is None else confidence,
        last_seen=last, evidence=tuple(ids[-5:]),
        stats={"samples": n, "evidence_label": label, "basis": "observed", "evidence_version": 2, **stats})


def mine_reminder_habits(facts_obs: list[Observation], notes: list[Observation], now: float,
                         tz: tzinfo) -> list[Pattern]:
    facts = _facts(facts_obs)
    out: list[Pattern] = []

    # Creation versus due time: how far ahead the user sets reminders, and when they set them.
    leads = [(f["due"] - f["created"], f) for f in facts
             if f["created"] is not None and f["due"] is not None and f["due"] >= f["created"]]
    if len(leads) >= REMINDER_MIN:
        median_h = statistics.median(v for v, _ in leads) / 3600
        parts = Counter(daypart(minute_of(local_dt(f["created"], tz))) for _, f in leads)
        part, cnt = parts.most_common(1)[0]
        text = f"You usually set a reminder about {_span(median_h)} before it is due"
        if cnt / len(leads) >= 0.4:
            text += f", mostly in the {part if part != 'night' else 'late evening'}"
        out.append(_pattern("lead_time", text + ".", n=len(leads), support=len(leads), opportunities=len(leads),
                            label=f"from {len(leads)} reminders with a creation and due time",
                            last=max(f["created"] for _, f in leads), ids=[f["id"] for _, f in leads],
                            median_hours=round(median_h, 2), daypart=part))

    # Completion lag: when finished reminders were ticked off relative to their due time.
    lags = [(f["done"] - f["due"], f) for f in facts if f["done"] is not None and f["due"] is not None]
    if len(lags) >= REMINDER_MIN:
        median_h = statistics.median(v for v, _ in lags) / 3600
        on_time = sum(1 for v, _ in lags if v <= 0)
        same_day = sum(1 for v, _ in lags if v <= 24 * 3600)
        text = (f"You usually finish a reminder about {_span(median_h)} {'after' if median_h > 0 else 'before'} "
                f"it is due" if abs(median_h) >= 1 / 60 else "You usually finish a reminder right when it is due")
        text += f"; {round(100 * on_time / len(lags))}% are done by their due time."
        out.append(_pattern("completion_lag", text, n=len(lags), support=on_time, opportunities=len(lags),
                            label=f"{on_time} of {len(lags)} finished reminders were done by their due time",
                            last=max(f["done"] for _, f in lags), ids=[f["id"] for _, f in lags],
                            median_hours=round(median_h, 2), on_time_pct=round(100 * on_time / len(lags)),
                            within_day_pct=round(100 * same_day / len(lags))))

    # Overdue rate: reminders that came due in the last eight weeks and are still open.
    due = [f for f in facts if f["due"] is not None and now - OVERDUE_LOOKBACK_S <= f["due"] < now - 3600]
    if len(due) >= REMINDER_MIN:
        open_ = sum(1 for f in due if f["done"] is None)
        out.append(_pattern("overdue", f"{round(100 * open_ / len(due))}% of the reminders that came due in the "
                            f"last eight weeks are still open ({open_} of {len(due)}).",
                            n=len(due), support=open_, opportunities=len(due),
                            label=f"{open_} of {len(due)} reminders due in the last eight weeks",
                            last=max(f["due"] for f in due), ids=[f["id"] for f in due],
                            open_pct=round(100 * open_ / len(due))))

    # Repeating reminders and repeating notes (a titled series such as dated entries).
    repeating = [f for f in facts if f["repeating"]]
    series = [o for o in notes if (o.attrs or {}).get("series") is True]
    parts = []
    if len(facts) >= REMINDER_MIN and repeating:
        parts.append(f"{len(repeating)} of your {len(facts)} reminders repeat")
    if len(notes) >= SERIES_MIN_NOTES and series:
        parts.append(f"{len(series)} of your {len(notes)} notes belong to a repeating series")
    if parts:
        n = (len(facts) if repeating else 0) + (len(notes) if series else 0)
        out.append(_pattern("repeating", "; ".join(parts).capitalize() + ".", n=n,
                            support=len(repeating) + len(series), opportunities=n,
                            label="; ".join(parts), last=max([f["created"] or f["due"] or 0 for f in repeating]
                                                             + [o.ts for o in series] + [0]) or None,
                            ids=[f["id"] for f in repeating] + [o.id for o in series],
                            repeating_reminders=len(repeating), reminders=len(facts),
                            series_notes=len(series), notes=len(notes)))
    return out


def mine_profile_habits(by_kind: dict[str, list[Observation]], now: float, tz: tzinfo) -> list[Pattern]:
    """Every habit pattern this module can produce from the lookback's observations."""
    out: list[Pattern] = []
    sends = {"email": by_kind.get("email_out", []),
             "message": by_kind.get("message_out", []),
             "note": [o for o in by_kind.get("note", []) if o.source == "notes"]}
    for channel, rows in sends.items():
        pattern = mine_send_timing(channel, rows, tz)
        if pattern is not None:
            out.append(pattern)
    out += mine_reminder_habits(by_kind.get("reminder_fact", []), sends["note"], now, tz)
    return out
