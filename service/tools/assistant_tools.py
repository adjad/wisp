"""Agent tools over the assistant commitments store.

These make schedule questions ("what's on my calendar today?", "what's due this
week?") answerable from the local store in milliseconds — instead of the agent
improvising AppleScript against Calendar.app, which is slow, permission-fragile
(the exact failure the user hit), and unaware of manual reminders. The store is
fed by the app's CalendarReader (EventKit) plus manual additions.
"""
from __future__ import annotations

import time
from datetime import date, datetime

from service.assistant.store import assistant_store
from service.tools.registry import register

_KIND_LABEL = {"exam": "EXAM", "assignment": "due", "meeting": "meeting",
               "event": "event", "reminder": "reminder"}


def _day_tag(event_day: date, today: date) -> str:
    """Absolute-safe day label the model can echo verbatim without doing any
    date arithmetic of its own — the whole point is that a small model (gemma)
    kept miscomputing which item was 'today'. Anchors every row explicitly."""
    delta = (event_day - today).days
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return "TOMORROW"
    if 2 <= delta <= 6:
        return event_day.strftime("%A")          # e.g. "Monday"
    return event_day.strftime("%a %b %-d")        # far out — full date


def _fmt(c: dict, now: float, *, show_account: bool = False) -> str:
    when = datetime.fromtimestamp(c["when_ts"])
    today = datetime.fromtimestamp(now).date()
    day = f"{_day_tag(when.date(), today)} ({when.strftime('%a %b %-d')})"
    clock = "all day" if c.get("all_day") else when.strftime("%-I:%M %p")
    delta = c["when_ts"] - now
    if delta < 0:
        rel = "now"
    elif delta < 3600:
        rel = f"in {int(delta // 60)} min"
    elif delta < 86400:
        rel = f"in {delta / 3600:.1f} h"
    else:
        rel = f"in {int(delta // 86400)} d"
    # `organizer` is a real person (never the user themselves — see
    # CalendarReader.swift) so it's shown as "with <person>". `context` is
    # just the calendar's name (e.g. "Work", or often the user's own name for
    # a personal calendar) — showing it the same way used to read as "meeting
    # with yourself". Only fall back to it when there's no real organizer.
    who = f" with {c['organizer']}" if c.get("organizer") else \
          (f" [{c['context']}]" if c.get("context") else "")
    loc = f" @ {c['location']}" if c.get("location") else ""
    label = _KIND_LABEL.get(c["kind"], c["kind"])
    # `account` (which linked Calendar account this came from — e.g. "iCloud"
    # vs a work Google calendar) is only worth showing when more than one is
    # actually in play; with a single account it'd just be noise on every row.
    acct = f" ({c['account']})" if show_account and c.get("account") else ""
    return f"- {day} {clock} ({rel}) {label}: {c['title']}{who}{loc}{acct}"


def _rel_past(delta_s: float) -> str:
    delta_s = abs(delta_s)
    if delta_s < 3600:
        return f"{int(delta_s // 60)} min ago"
    if delta_s < 86400:
        return f"{delta_s / 3600:.1f} h ago"
    return f"{int(delta_s // 86400)} d ago"


def _fmt_past(c: dict, now: float, *, show_account: bool = False) -> str:
    when = datetime.fromtimestamp(c["when_ts"])
    clock = "all day" if c.get("all_day") else when.strftime("%-I:%M %p")
    who = f" with {c['organizer']}" if c.get("organizer") else \
          (f" [{c['context']}]" if c.get("context") else "")
    loc = f" @ {c['location']}" if c.get("location") else ""
    label = _KIND_LABEL.get(c["kind"], c["kind"])
    acct = f" ({c['account']})" if show_account and c.get("account") else ""
    rel = _rel_past(c["when_ts"] - now)
    return (f"- {when.strftime('%a %b %-d, %Y')} {clock} ({rel}) {label}: "
            f"{c['title']}{who}{loc}{acct}")


def _filter_account(items: list[dict], account: str | None) -> list[dict]:
    if not account:
        return items
    q = account.lower()
    return [c for c in items if q in (c.get("account") or "").lower()]


@register(
    "get_upcoming",
    "Read the user's upcoming schedule — calendar events, meetings, Canvas "
    "assignment/exam due dates, and reminders — from Wisp's local commitment "
    "store. ALWAYS use this for questions about the calendar, schedule, "
    "deadlines, what's due, or what's coming up. Pass `account` if the user "
    "asks about a SPECIFIC linked calendar account and more than one is "
    "linked. Do NOT script Calendar.app.",
    {"type": "object",
     "properties": {
         "days": {"type": "integer",
                  "description": "how many days ahead to look (default 7, max 60)"},
         "account": {"type": "string",
                     "description": "only include this linked calendar account (only useful when more than one is linked)"},
     }},
    category="assistant_read",
)
async def get_upcoming(days: int = 7, account: str | None = None) -> str:
    days = max(1, min(int(days or 7), 60))
    now = time.time()
    # Anchor "today" IN the tool output so the narrating model never has to
    # infer the current date (a verified gemma failure — it mislabeled Jul 17
    # items as "today"). Each row is also tagged TODAY/TOMORROW/<weekday>.
    today_str = datetime.fromtimestamp(now).strftime("%A, %B %-d, %Y")
    items = _filter_account(assistant_store.upcoming(now=now, days=days), account)
    if not items:
        return (f"Today is {today_str}. Nothing scheduled in the next {days} day(s). "
                "(If real calendar events are missing, Calendar access may not "
                "be granted to Wisp, or the first sync hasn't run yet.)")
    show_account = len({c.get("account") for c in items if c.get("account")}) > 1
    lines = [_fmt(c, now, show_account=show_account) for c in items]
    return (f"Today is {today_str}. Upcoming ({days}d window, {len(items)} item(s)) — "
            "each row is tagged relative to today:\n" + "\n".join(lines))


@register(
    "get_past_events",
    "Read what was on the user's calendar in the PAST — up to about a year "
    "back. Use for questions like 'what did I have last week' / 'when did I "
    "last meet with X' / 'what was on my calendar in March'. Pass `account` "
    "if the user asks about a SPECIFIC linked calendar account and more than "
    "one is linked. This is the past-facing counterpart to get_upcoming.",
    {"type": "object",
     "properties": {
         "days": {"type": "integer",
                  "description": "how many days back to look (default 30, max 365)"},
         "query": {"type": "string",
                   "description": "keyword to filter titles/context by (e.g. a person or project name)"},
         "account": {"type": "string",
                     "description": "only include this linked calendar account (only useful when more than one is linked)"},
     }},
    category="assistant_read",
)
async def get_past_events(days: int = 30, query: str | None = None,
                          account: str | None = None) -> str:
    days = max(1, min(int(days or 30), 365))
    now = time.time()
    today_str = datetime.fromtimestamp(now).strftime("%A, %B %-d, %Y")
    items = _filter_account(assistant_store.history(now=now, days=days), account)
    if query:
        q = query.lower()
        items = [c for c in items if q in c["title"].lower() or q in (c.get("context") or "").lower()]
    if not items:
        return (f"Today is {today_str}. Nothing found in the past {days} day(s)"
                + (f" matching {query!r}" if query else "") + ". "
                "(Calendar history only reaches back to whenever Wisp started "
                "syncing it, up to about a year.)")
    show_account = len({c.get("account") for c in items if c.get("account")}) > 1
    lines = [_fmt_past(c, now, show_account=show_account) for c in items]
    return (f"Today is {today_str}. Past {days} day(s), {len(items)} item(s), most recent first:\n"
            + "\n".join(lines))


@register(
    "add_reminder",
    "Create a reminder in Wisp's local commitment store. Wisp will notify the "
    "user at the right time and show a countdown. Use when the user asks to be "
    "reminded of something or to track a deadline.",
    {"type": "object",
     "properties": {
         "title": {"type": "string", "description": "what to remind about"},
         "when_iso": {"type": "string",
                      "description": "local datetime ISO format, e.g. 2026-07-13T16:00"},
         "kind": {"type": "string",
                  "enum": ["reminder", "assignment", "exam", "meeting", "event"],
                  "description": "defaults to reminder"},
     },
     "required": ["title", "when_iso"]},
    category="assistant_write",
)
async def add_reminder(title: str, when_iso: str, kind: str = "reminder") -> str:
    try:
        when = datetime.fromisoformat(when_iso)
    except ValueError:
        return f"(bad when_iso {when_iso!r} — use e.g. 2026-07-13T16:00)"
    ts = when.timestamp()
    if ts < time.time() - 60:
        return f"({when_iso} is in the past — not added)"
    c = assistant_store.add_manual(title.strip(), ts, kind=kind or "reminder")
    try:
        from service.assistant.hub import hub
        # nudge the UI so the countdown chip updates immediately, AND ask the app
        # to mirror this into the macOS Reminders app (it holds that grant).
        await hub.publish({"type": "changed"})
        await hub.publish({"type": "create_apple_reminder",
                           "title": c["title"], "when_ts": ts})
    except Exception:  # noqa: BLE001
        pass
    when_str = when.strftime("%a %b %-d at %-I:%M %p")
    return f"Reminder set: “{c['title']}” — {when_str} (also added to Apple Reminders)."


@register(
    "add_calendar_event",
    "Create a REAL event in the user's macOS Calendar (it syncs to their other "
    "devices via iCloud/Google). Use this when the user wants an actual calendar "
    "event or meeting — as opposed to add_reminder, which only makes a private "
    "Wisp reminder. The Wisp app performs the write, so it also appears in the "
    "upcoming list on the next sync.",
    {"type": "object",
     "properties": {
         "title": {"type": "string"},
         "when_iso": {"type": "string",
                      "description": "local start datetime, e.g. 2026-07-14T15:00"},
         "duration_min": {"type": "integer", "description": "length in minutes (default 60)"},
         "location": {"type": "string", "description": "optional location"},
     },
     "required": ["title", "when_iso"]},
    category="assistant_write",
)
async def add_calendar_event(title: str, when_iso: str,
                             duration_min: int = 60, location: str = "") -> str:
    try:
        when = datetime.fromisoformat(when_iso)
    except ValueError:
        return f"(bad when_iso {when_iso!r} — use e.g. 2026-07-14T15:00)"
    if when.timestamp() < time.time() - 60:
        return f"({when_iso} is in the past — not added)"
    # The macOS Calendar grant lives in the Swift app; ask it to do the write
    # over the assistant SSE channel. It re-syncs afterwards so the event lands
    # in the store/countdown chip.
    from service.assistant.hub import hub
    await hub.publish({
        "type": "create_calendar_event",
        "title": title.strip(),
        "when_ts": when.timestamp(),
        "duration_min": int(duration_min or 60),
        "location": location or "",
    })
    when_str = when.strftime("%a %b %-d at %-I:%M %p")
    return (f"Added “{title}” to your calendar for {when_str}. "
            "(If it doesn't appear, make sure the Wisp app is running and has "
            "Calendar access.)")


@register(
    "cancel_event",
    "Cancel or delete an upcoming event or reminder, matched by part of its "
    "title. Removes real calendar events from macOS Calendar too. Use when the "
    "user wants to cancel, delete, or remove something from their schedule.",
    {"type": "object",
     "properties": {
         "title": {"type": "string",
                   "description": "the event/reminder to cancel (any distinctive part of its title)"},
     },
     "required": ["title"]},
    category="assistant_write",
)
async def cancel_event(title: str) -> str:
    q = title.lower().strip()
    matches = [c for c in assistant_store.upcoming(days=60) if q in c["title"].lower()]
    if not matches:
        return f"Nothing upcoming matches “{title}”."
    if len(matches) > 1:
        lst = "; ".join(
            f"{c['title']} ({datetime.fromtimestamp(c['when_ts']).strftime('%a %-I:%M %p')})"
            for c in matches[:5])
        return f"Several items match “{title}”: {lst}. Which one? Please be more specific."
    c = matches[0]
    from service.assistant.hub import hub
    if c["source"] == "calendar" and c.get("source_id"):
        await hub.publish({"type": "delete_calendar_event", "source_id": c["source_id"]})
        assistant_store.set_status(c["id"], "dismissed")
    else:
        assistant_store.delete(c["id"])
    await hub.publish({"type": "changed"})
    return f"Cancelled “{c['title']}”."
