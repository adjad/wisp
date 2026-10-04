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
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from service.assistant.store import AssistantStore
from service.tools.registry import register

_store = AssistantStore()


def _availability_timezone() -> str | None:
    """Use the process/system IANA zone, never a PDT/+14 abbreviation."""
    zone = os.environ.get("TZ", "").lstrip(":")
    if not zone:
        parts = Path("/etc/localtime").resolve().parts
        for index, part in enumerate(parts):
            if part.startswith("zoneinfo"):
                zone = "/".join(parts[index + 1:])
                break
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return zone


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
            "expected_id": {"type": "string",
                            "description": "optional typed-plan guard: exact visible reminder id selected before execution"},
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
async def complete_reminder(title: str, expected_id: str = "") -> str:
    needle = (title or "").strip().lower()
    if not needle:
        return "(error: complete_reminder needs part of the reminder's `title`.)"

    reminder_sources = {"manual", "reminders"}
    rows = [row for row in _store.active_future(horizon_days=365)
            if row.get("source") in reminder_sources
            or bool(reminder_sources & set(row.get("duplicate_sources") or []))]
    hits = [r for r in rows if needle in (r.get("title") or "").lower()]
    exact = [r for r in hits if " ".join((r.get("title") or "").split()).casefold()
             == " ".join(title.split()).casefold()]
    if exact:
        hits = exact
    if expected_id:
        hits = [row for row in hits
                if str(row.get("id") or "") == expected_id
                or expected_id in {str(value) for value in row.get("duplicate_ids") or []}]
    if not hits:
        if expected_id:
            return ("(error: the selected reminder changed before it could be completed; "
                    "nothing was changed.)")
        return (f"Nothing active matches {title!r}. "
                f"Ask me what's upcoming if you're not sure of the wording.")
    if len(hits) > 1:
        listed = "\n".join(f"  - {r['title']} ({_fmt_time(r['when_ts'])})" for r in hits[:8])
        return f"Several match {title!r} — which one?\n{listed}"

    row = hits[0]
    group = [row] + [member for member in
                     (_store.get(cid) for cid in row.get("duplicate_ids") or []) if member]
    native = [member for member in group if member.get("source") == "reminders"]
    if len(native) > 1:
        return "(error: several native reminders share this item; select one exact reminder before completing.)"
    if native:
        from service.assistant.outbox import request as app_request
        for member in native:
            if not member.get("source_id") or member.get("when_ts") is None:
                return "(error: exact Reminders identity is unavailable; nothing was marked done.)"
            result = await app_request("complete_reminder", {
                "source_id": member["source_id"], "expected_title": member["title"],
                "expected_due_ts": member["when_ts"]})
            if result.get("ok") is not True:
                return f"(error: {result.get('error') or 'Native completion was not verified'}.)"
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
    "commitments get_upcoming reports, plus pinned Wisp Today tasks.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer",
                     "description": "How many days ahead to consider. Default 3."},
            "period": {"type": "string", "description":
                       "One exact local day: today, tomorrow, or YYYY-MM-DD. "
                       "Overrides days; returns duration-specific candidate slots."},
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
async def find_free_time(days: int = 3, minutes: int = 30,
                         day_start: int = 9, day_end: int = 18,
                         period: str = "") -> str:
    try:
        days = max(1, min(14, int(days)))
        minutes = int(minutes)
        if not 1 <= minutes <= 1440:
            return "(error: minutes must be between 1 and 1440.)"
        need = minutes * 60
        start_h = max(0, min(23, int(day_start)))
        end_h = max(start_h + 1, min(24, int(day_end)))
    except (TypeError, ValueError):
        return "(error: days/minutes/day_start/day_end must be numbers.)"

    now = dt.datetime.now()
    today = now.date()
    if period:
        import re
        from service.tools.timeranges import resolve_span, BadPeriod
        if not re.fullmatch(r"today|tomorrow|\d{4}-\d{2}-\d{2}", period.strip(), re.I):
            return "(error: period must be today, tomorrow, or YYYY-MM-DD.)"
        try:
            begin, _, _ = resolve_span(period, now=now)
        except BadPeriod as exc:
            return f"(error: {exc})"
        target_days = [dt.datetime.fromtimestamp(begin).date()]
    else:
        target_days = [today + dt.timedelta(days=offset) for offset in range(days)]

    from service.assistant.sync_status import ensure_sources
    from service.tasks.temporal import unambiguous_local_time
    readiness = await ensure_sources(("calendar", "reminders"))
    if (any(s["state"] != "ready" for s in readiness["sources"])
            or not readiness.get("reminders_fresh", True)):
        return ("(error: Wisp could not check current Calendar and Reminders availability. "
                "No study slot has been verified; try again after the sources finish syncing.)")
    timezone = _availability_timezone()
    if timezone is None:
        return "(error: the local timezone could not be resolved; no availability was verified.)"

    out: list[str] = []
    assumed_duration = False
    for day in target_days:
        window_lo = dt.datetime.combine(day, dt.time(start_h))
        window_hi = dt.datetime.combine(day, dt.time(0)) + dt.timedelta(hours=end_h)
        if not (unambiguous_local_time(window_lo) and unambiguous_local_time(window_hi)):
            return "(error: the working window includes an ambiguous or nonexistent local time.)"
        lo = max(window_lo, now) if day == today else window_lo
        if lo >= window_hi:
            continue

        # The snapshot's overlap-aware read includes overnight Calendar events
        # and their stored end times. Clip every block to this local day/window.
        snapshot = _store.today_snapshot(day.isoformat(), timezone)
        rows = list(snapshot["commitments"])
        for task in snapshot.get("tasks", []):
            if task.get("status") not in (None, "active") or task.get("pinned_start") is None:
                continue
            rows.append({"when_ts": task["pinned_start"], "end_ts":
                task["pinned_start"] + task["duration_minutes"] * 60, "source": "today"})
        blocks = []
        for row in rows:
            if row.get("status") not in (None, "active") or row.get("when_ts") is None:
                continue
            begin = float(row["when_ts"])
            finish = row.get("end_ts")
            if row.get("all_day") and finish is None:
                event_day = dt.datetime.fromtimestamp(begin).date()
                finish = dt.datetime.combine(event_day + dt.timedelta(days=1), dt.time()).timestamp()
            elif finish is None:
                finish = begin + 3600
                if begin < window_hi.timestamp() and finish > lo.timestamp():
                    assumed_duration = True
            a, b = max(begin, lo.timestamp()), min(float(finish), window_hi.timestamp())
            if a < b:
                blocks.append((a, b))
        blocks.sort()
        gaps: list[tuple[float, float]] = []
        cursor = lo.timestamp()
        for b_start, b_end in blocks:
            if b_start > cursor and b_start - cursor >= need:
                gaps.append((cursor, b_start))
            cursor = max(cursor, b_end)
        if window_hi.timestamp() - cursor >= need:
            gaps.append((cursor, window_hi.timestamp()))

        if gaps:
            spans = ", ".join(f"{_fmt_time(a)}–{_fmt_time(a + need if period else b)}" for a, b in gaps)
            label = day.strftime("%A, %Y-%m-%d") if period else _fmt_day(day, today)
            out.append(f"  {label}: {spans}")

    if not out:
        scope = target_days[0].strftime("%A, %Y-%m-%d") if period else f"the next {days} day(s)"
        return (f"No matches: No gaps of {minutes}+ minutes in {scope} "
                f"between {start_h}:00 and {end_h}:00.")
    note = ("\n(Items without a stored end time are assumed to run an hour.)"
            if assumed_duration else "")
    note += "\nThese are candidate times; a Calendar event or reminder has not been created."
    heading = f"{minutes}-minute candidate slots" if period else f"Free for {minutes}+ minutes"
    return (f"{heading}, {start_h}:00–{end_h}:00:\n"
            + "\n".join(out) + note)
