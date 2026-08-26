"""Completing reminders, and finding a gap in the day.

`complete_reminder` vs `cancel_event` — WHY BOTH
------------------------------------------------
`cancel_event` DELETES. Marking something done is a different act with a
different meaning: the commitment happened, and the record of it should survive.
Routing "I've done that" to a delete tool destroys the history the store exists
to keep, and it is not recoverable. The store already distinguishes them —
`set_status(cid, "done")` against `delete(cid)` — so this is exposing a
distinction that existed rather than inventing one.

`find_free_time` computes gaps from the same commitment store `get_upcoming`
reads, so it agrees with what the user was just told about their day. It does
not consult EventKit directly: that would be a second source of truth for the
same question, and the two would drift the moment a sync lagged.
"""
from __future__ import annotations

import datetime as dt

from service.assistant.store import AssistantStore
from service.tools.registry import register

_store = AssistantStore()


def _fmt_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%-I:%M %p")


def _fmt_day(d: dt.date, today: dt.date) -> str:
    if d == today:
        return "today"
    if d == today + dt.timedelta(days=1):
        return "tomorrow"
    return d.strftime("%A %-d %b")


@register(
    "complete_reminder",
    "Mark a reminder or task DONE, matched by part of its title. Use this when "
    "the user says they've finished something — NOT cancel_event, which deletes "
    "the record entirely. If several match, it asks which rather than guessing.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string",
                      "description": "Any distinctive part of the reminder's title."},
        },
        "required": ["title"],
    },
    category="assistant_write",
    # The pronoun-only forms ("cross it off", "that's sorted") matter most and
    # are the hardest: with no noun to anchor, they sit in embedding space right
    # next to delete_path, cancel_event and archive_email, which mean something
    # materially different. Measured — without these, "that's sorted, cross it
    # off" ranked complete_reminder 11th, behind four tools that would have
    # destroyed the record instead of completing it.
    aliases=["I've done that already", "mark the dentist thing as done",
             "tick off the laundry reminder", "finished the report, check it off",
             "that one's handled now", "I took my medication",
             "cross it off my list", "that's done, sorted",
             "check that one off"],
)
def complete_reminder(title: str) -> str:
    needle = (title or "").strip().lower()
    if not needle:
        return "(error: complete_reminder needs part of the reminder's `title`.)"

    rows = _store.active_future(horizon_days=365)
    hits = [r for r in rows if needle in (r.get("title") or "").lower()]
    if not hits:
        return (f"Nothing active matches {title!r}. "
                f"Ask me what's upcoming if you're not sure of the wording.")
    if len(hits) > 1:
        listed = "\n".join(f"  - {r['title']} ({_fmt_time(r['when_ts'])})" for r in hits[:8])
        return f"Several match {title!r} — which one?\n{listed}"

    row = hits[0]
    ok = _store.set_status(row["id"], "done")
    # Duplicates are one commitment seen through several sources (a calendar
    # event that is also a reminder). Leaving the siblings active would make the
    # item reappear on the next read, looking like the completion didn't stick.
    for dup in row.get("duplicate_ids") or []:
        _store.set_status(dup, "done")
    if not ok:
        return f"(couldn't update {row['title']!r} — it may already be gone.)"
    return f"Marked “{row['title']}” done."


@register(
    "find_free_time",
    "Find open gaps in the user's schedule — for 'when am I free', 'do I have "
    "time for a call', or picking a slot for something. Looks at the same "
    "commitments get_upcoming reports.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer",
                     "description": "How many days ahead to consider. Default 3."},
            "minutes": {"type": "integer",
                        "description": "Minimum length of gap to report, in minutes. Default 30."},
            "day_start": {"type": "integer",
                          "description": "Earliest hour to count as available, 0-23. Default 9."},
            "day_end": {"type": "integer",
                        "description": "Latest hour, 0-23. Default 18."},
        },
    },
    category="assistant_read",
    aliases=["when am I free this week", "do I have a gap tomorrow afternoon",
             "find me an hour for the gym", "when could I fit in a call",
             "what does my afternoon look like", "am I free at 3"],
)
def find_free_time(days: int = 3, minutes: int = 30,
                   day_start: int = 9, day_end: int = 18) -> str:
    try:
        days = max(1, min(14, int(days)))
        need = max(5, int(minutes)) * 60
        start_h = max(0, min(23, int(day_start)))
        end_h = max(start_h + 1, min(24, int(day_end)))
    except (TypeError, ValueError):
        return "(error: days/minutes/day_start/day_end must be numbers.)"

    now = dt.datetime.now()
    today = now.date()
    rows = _store.upcoming(days=days)

    # Busy blocks, keyed by day. All-day items block the whole window — treating
    # them as zero-length would advertise a free afternoon on a day the user has
    # marked entirely spoken for.
    busy: dict[dt.date, list[tuple[float, float]]] = {}
    for r in rows:
        if r.get("status") not in (None, "active"):
            continue
        ts = r.get("when_ts")
        if not ts:
            continue
        begin = dt.datetime.fromtimestamp(ts)
        day = begin.date()
        if r.get("all_day"):
            lo = dt.datetime.combine(day, dt.time(start_h))
            hi = dt.datetime.combine(day, dt.time(0)) + dt.timedelta(hours=end_h)
            busy.setdefault(day, []).append((lo.timestamp(), hi.timestamp()))
            continue
        # The store keeps a start time and no duration, so assume an hour. Said
        # out loud in the output rather than silently — a 15-minute reminder
        # will look like it blocks more than it does.
        busy.setdefault(day, []).append((ts, ts + 3600))

    out: list[str] = []
    for offset in range(days):
        day = today + dt.timedelta(days=offset)
        window_lo = dt.datetime.combine(day, dt.time(start_h))
        window_hi = dt.datetime.combine(day, dt.time(0)) + dt.timedelta(hours=end_h)
        lo = max(window_lo, now) if day == today else window_lo
        if lo >= window_hi:
            continue

        blocks = sorted(busy.get(day, []))
        gaps: list[tuple[float, float]] = []
        cursor = lo.timestamp()
        for b_start, b_end in blocks:
            if b_start > cursor and b_start - cursor >= need:
                gaps.append((cursor, b_start))
            cursor = max(cursor, b_end)
        if window_hi.timestamp() - cursor >= need:
            gaps.append((cursor, window_hi.timestamp()))

        if gaps:
            spans = ", ".join(f"{_fmt_time(a)}–{_fmt_time(b)}" for a, b in gaps)
            out.append(f"  {_fmt_day(day, today)}: {spans}")

    if not out:
        return (f"No gaps of {minutes}+ minutes in the next {days} day(s) "
                f"between {start_h}:00 and {end_h}:00.")
    note = ("\n(Timed items are assumed to run an hour — the calendar store "
            "keeps start times, not lengths.)")
    return (f"Free for {minutes}+ minutes, {start_h}:00–{end_h}:00:\n"
            + "\n".join(out) + note)
