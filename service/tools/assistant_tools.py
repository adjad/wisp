"""Agent tools over the assistant commitments store.

These make schedule questions ("what's on my calendar today?", "what's due this
week?") answerable from the local store in milliseconds — instead of the agent
improvising AppleScript against Calendar.app, which is slow, permission-fragile
(the exact failure the user hit), and unaware of manual reminders. The store is
fed by the app's CalendarReader (EventKit) plus manual additions.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta

from service.assistant.store import assistant_store
from service.tools.registry import EVENT_UPDATE_UNAVAILABLE, register

_KIND_LABEL = {"exam": "EXAM", "assignment": "due", "meeting": "meeting",
               "event": "event", "reminder": "reminder"}


def _day_tag(event_day: date, today: date) -> str:
    """Absolute-safe day label the model can echo verbatim without doing any
    date arithmetic of its own — the whole point is that a small model (the summarizer)
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
    tag, absolute = _day_tag(when.date(), today), when.strftime('%a %b %-d')
    # A week or more out `_day_tag` already IS the absolute date, and printing
    # it twice ("Thu Sep 17 (Thu Sep 17)") is what a delivered schedule read as
    # repeated events.
    day = absolute if tag == absolute else f"{tag} ({absolute})"
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


_HOLIDAY_CALENDAR_RE = re.compile(r"\bholidays?\b", re.I)


def _is_holiday_calendar_item(item: dict) -> bool:
    """True only for events supplied by a calendar explicitly named Holidays.

    Filtering by the calendar name, rather than by event titles such as
    "Christmas" or "Labor Day", keeps similarly named personal events intact.
    The macOS subscribed calendar in the reported trace is named
    ``US Holidays`` and stores that name in ``context``.
    """
    if item.get("source") != "calendar":
        return False
    calendar_names = (item.get("context"), item.get("calendar"),
                      item.get("calendar_name"))
    return any(_HOLIDAY_CALENDAR_RE.search(str(name or ""))
               for name in calendar_names)


def _without_holiday_calendars(items: list[dict], *, include_holidays: bool) -> list[dict]:
    if include_holidays:
        return items
    return [item for item in items if not _is_holiday_calendar_item(item)]


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
         "period": {"type": "string", "description": "Exact calendar range, e.g. tomorrow, this week, this month. Overrides days."},
         "calendar_only": {"type": "boolean", "description": "Only actual calendar events, not preparation reminders."},
         "query": {"type": "string", "description": "Filter titles within the requested range."},
         "account": {"type": "string",
                     "description": "only include this linked calendar account (only useful when more than one is linked)"},
         "include_holidays": {"type": "boolean",
                     "description": "include subscribed holiday calendars only when the user explicitly asks for holidays; default false"},
     }},
    category="assistant_read",
)
async def get_upcoming(days: int = 7, account: str | None = None,
                       include_holidays: bool = False, period: str = "",
                       calendar_only: bool = False, query: str = "") -> str:
    from service.assistant.sync_status import ensure_sources
    readiness = await ensure_sources(("calendar", "reminders"))
    pending = [s["label"].lower() for s in readiness["sources"]
               if s["state"] == "syncing"]
    if pending:
        names = " and ".join(pending)
        return (f"Wisp is still syncing your {names} after launch, so I’m "
                "holding off rather than showing an incomplete schedule. "
                "Try again in a moment.")
    days = max(1, min(int(days or 7), 60))
    now = time.time()
    # Anchor "today" IN the tool output so the narrating model never has to
    # infer the current date (a verified the summarizer failure — it mislabeled Jul 17
    # items as "today"). Each row is also tagged TODAY/TOMORROW/<weekday>.
    today_str = datetime.fromtimestamp(now).strftime("%A, %B %-d, %Y")
    window_label = f"next {days} day(s)"
    if period:
        from service.tools.timeranges import resolve_span, BadPeriod
        try:
            start, end, window_label = resolve_span(period)
        except BadPeriod as exc:
            return f"(error: {exc})"
        rows = [row for row in assistant_store.active_between(start, end)
                if start <= float(row.get("when_ts") or 0) < end]
    else:
        rows = assistant_store.upcoming(now=now, days=days)
    items = _filter_account(rows, account)
    if calendar_only:
        items = [row for row in items if row.get("source") == "calendar"
                 or "calendar" in (row.get("duplicate_sources") or [])]
    if query:
        normalize = lambda value: re.sub(r"[\W_]+", "", value.casefold())
        items = [row for row in items if normalize(query) in normalize(str(row.get("title") or ""))]
    items = _without_holiday_calendars(items, include_holidays=bool(include_holidays))
    unavailable = [s for s in readiness["sources"] if s["state"] == "unavailable"]
    unavailable_ids = {s["id"] for s in unavailable}
    items = [item for item in items if item.get("source") not in unavailable_ids]
    notice = ("Wisp could not check " + " and ".join(s["label"] for s in unavailable)
              + ". Check its access in Settings; this schedule may be incomplete.\n") if unavailable else ""
    if not items:
        if notice:
            return notice + "No scheduled items were found in the sources that could be checked."
        return (f"Today is {today_str}. Nothing scheduled in {window_label}. "
                "(If real calendar events are missing, Calendar access may not "
                "be granted to Wisp, or the first sync hasn't run yet.)")
    show_account = len({c.get("account") for c in items if c.get("account")}) > 1
    def sources(c: dict) -> set[str]:
        return {c.get("source", "")} | set(c.get("duplicate_sources") or [])

    def source_label(c: dict) -> str:
        tags = []
        origins = sources(c)
        if "calendar" in origins:
            tags.append("Calendar event")
        if "reminders" in origins:
            tags.append("Apple Reminder")
        elif "manual" in origins:
            tags.append("Wisp reminder; Apple mirror not verified")
        return f" [{' / '.join(tags)}]" if tags else ""

    calendar_count = sum("calendar" in sources(c) for c in items)
    reminder_count = sum(bool(sources(c) & {"manual", "reminders"}) for c in items)
    lines = [_fmt(c, now, show_account=show_account) + source_label(c) for c in items]
    return (notice + f"Today is {today_str}. Upcoming ({window_label}, {len(items)} item(s)) — "
            "each row is tagged relative to today:\n"
            + f"Calendar events: {calendar_count}; Wisp/Apple reminders: {reminder_count}. "
            "A calendar event alone is not a reminder.\n" + "\n".join(lines))


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
         "include_holidays": {"type": "boolean",
                     "description": "include subscribed holiday calendars only when the user explicitly asks for holidays; default false"},
     }},
    category="assistant_read",
)
async def get_past_events(days: int = 30, query: str | None = None,
                          account: str | None = None,
                          include_holidays: bool = False) -> str:
    from service.assistant.sync_status import ensure_sources
    readiness = await ensure_sources(("calendar",))
    if any(s["state"] == "syncing" for s in readiness["sources"]):
        return ("Wisp is still syncing your calendar after launch, so I’m "
                "holding off rather than showing incomplete history. Try "
                "again in a moment.")
    if any(s["state"] == "unavailable" for s in readiness["sources"]):
        return "Wisp could not check Calendar. Check Calendar access in Settings, then try again."
    days = max(1, min(int(days or 30), 365))
    now = time.time()
    today_str = datetime.fromtimestamp(now).strftime("%A, %B %-d, %Y")
    items = _filter_account(assistant_store.history(now=now, days=days), account)
    items = _without_holiday_calendars(items, include_holidays=bool(include_holidays))
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
    "reminded of something, asks for an alarm/nudge, or to track a deadline. "
    "This creates a Wisp notification and requests an Apple Reminders mirror, "
    "not a ringing Clock alarm. A calendar appointment or saved memory is not "
    "a reminder. Ask for the alert time if it wasn't specified.",
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
    mirror_requested = False
    try:
        from service.assistant.hub import hub
        # nudge the UI so the countdown chip updates immediately, AND ask the app
        # to mirror this into the macOS Reminders app (it holds that grant).
        await hub.publish({"type": "changed"})
        await hub.publish({"type": "create_apple_reminder",
                           "title": c["title"], "when_ts": ts})
        mirror_requested = True
    except Exception:  # noqa: BLE001
        pass
    when_str = when.strftime("%a %b %-d at %-I:%M %p")
    mirror = ("Apple Reminders sync requested" if mirror_requested
              else "Apple Reminders sync could not be requested")
    return f"Reminder set: “{c['title']}” — {when_str} in Wisp ({mirror})."


@register(
    "update_reminder",
    "Reschedule or rename an existing Wisp/Apple reminder. Match it by part "
    "of its current title. For an immediate correction such as 'I mean today' "
    "right after creating a reminder, omit title and the most recently created "
    "reminder is updated. Supply either when_iso for an exact new time or day="
    "'today'/'tomorrow' to keep its existing time of day on that date.",
    {"type": "object",
     "properties": {
         "title": {"type": "string",
                   "description": "Current title text to match. Omit only for an immediate correction of the latest reminder."},
         "when_iso": {"type": "string",
                      "description": "Exact new local datetime, e.g. 2026-08-27T09:00."},
         "day": {"type": "string", "enum": ["today", "tomorrow"],
                 "description": "Move to this local day while preserving the existing time of day."},
         "new_title": {"type": "string", "description": "Optional replacement title."},
         "expected_id": {"type": "string",
                         "description": "optional typed-plan guard: exact visible reminder id selected before execution"},
     },
     "required": []},
    category="assistant_write",
    aliases=["I mean today", "make that reminder today instead",
             "move my reminder to tomorrow", "change the reminder time",
             "reschedule my reminder"],
)
async def update_reminder(title: str = "", when_iso: str = "", day: str = "",
                          new_title: str = "", expected_id: str = "") -> str:
    if not when_iso and day not in {"today", "tomorrow"}:
        return ("(error: pass when_iso, or day='today'/'tomorrow'; "
                "the reminder was not changed.)")

    candidates = reminders_matching("all", title)
    if expected_id:
        candidates = [c for c in candidates
                      if str(c.get("id") or "") == expected_id
                      or expected_id in {str(value) for value in c.get("duplicate_ids") or []}]
    if not candidates:
        return (("(error: the selected reminder changed before it could be updated; "
                 "nothing was changed.)") if expected_id else
                f"Nothing active matches reminder {title!r}." if title.strip()
                else "Nothing active matches the reminder correction.")

    if title.strip():
        exact = [c for c in candidates
                 if " ".join(c["title"].split()).casefold()
                 == " ".join(title.split()).casefold()]
        if exact:
            candidates = exact
        if len(candidates) > 1:
            choices = "; ".join(
                f"{c['title']} ({datetime.fromtimestamp(c['when_ts']).strftime('%a %-I:%M %p')})"
                for c in candidates[:5])
            return f"Several reminders match “{title}”: {choices}. Which one?"
        current = candidates[0]
    else:
        # This branch is intentionally limited by the tool description and the
        # router's correction fast path to immediate follow-ups. ``created_at``
        # identifies what Wisp just created without asking the small model to
        # reconstruct a title from conversational prose.
        current = max(candidates,
                      key=lambda c: (c.get("created_at") or 0.0,
                                     c.get("updated_at") or 0.0,
                                     c.get("id") or ""))

    old_when = float(current["when_ts"])
    if when_iso:
        try:
            target = datetime.fromisoformat(when_iso)
        except ValueError:
            return f"(error: bad when_iso {when_iso!r} — use e.g. 2026-08-27T09:00)"
    else:
        old_local = datetime.fromtimestamp(old_when)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        target_day = today + timedelta(days=1 if day == "tomorrow" else 0)
        target = target_day.replace(hour=old_local.hour, minute=old_local.minute)

    new_when = target.timestamp()
    if new_when < time.time() - 60:
        return (f"(error: preserving the old time would put the reminder in the past "
                f"({target:%a %b %-d at %-I:%M %p}); ask what time today to use. "
                "The reminder was not changed.)")

    group = [current] + [row for row in
                         (assistant_store.get(cid)
                          for cid in current.get("duplicate_ids") or []) if row]
    final_title = new_title.strip() or current["title"]
    ids = [row["id"] for row in group]
    if assistant_store.update_schedule(ids, new_when, final_title) < 1:
        return "(error: the reminder changed before it could be updated; try again.)"

    from service.assistant.hub import hub
    await hub.publish({"type": "changed"})
    reminder_rows = [row for row in group if row.get("source") == "reminders"]
    if reminder_rows:
        for row in reminder_rows:
            await hub.publish({
                "type": "update_apple_reminder",
                "source_id": row.get("source_id") or "",
                "old_title": row.get("title") or current["title"],
                "old_when_ts": old_when,
                "title": final_title,
                "when_ts": new_when,
            })
    elif current.get("source") == "manual":
        # The immediate-correction case often arrives before RemindersWriter's
        # next sync has supplied the EventKit identifier. The app can still
        # locate the just-created item by its old title and due minute.
        await hub.publish({
            "type": "update_apple_reminder", "source_id": "",
            "old_title": current["title"], "old_when_ts": old_when,
            "title": final_title, "when_ts": new_when,
        })

    return f"Reminder updated: “{final_title}” — {target:%a %b %-d at %-I:%M %p}."


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
    # calendar_write, not assistant_write — see policy.py's
    # _ALWAYS_CONFIRM_CALENDAR for why this and cancel_event are gated
    # separately from add_reminder (2026-08-18: a bulk operation created a
    # wrongly-dated duplicate event with no confirmation of any kind).
    category="calendar_write",
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
    "update_event",
    "Unavailable: Wisp cannot safely preserve an existing calendar event's "
    "details when changing it. No update will run; edit the event in Calendar.",
    {"type": "object",
     "properties": {
         "title": {"type": "string", "description": "Existing title (compatibility only; updates are unavailable)."},
         "when_iso": {"type": "string", "description": "Requested start (compatibility only; no change will run)."},
         "duration_min": {"type": "integer", "description": "Compatibility only; ignored without changing the event."},
         "location": {"type": "string", "description": "Compatibility only; ignored without changing the event."},
         "new_title": {"type": "string", "description": "Compatibility only; ignored without changing the event."},
     },
     "required": ["title", "when_iso"]},
    category="calendar_write",
    aliases=["move my dentist appointment to 3pm", "reschedule the meeting to tomorrow",
             "push my lunch back an hour", "change the location of the standup"],
    unavailable_reason=EVENT_UPDATE_UNAVAILABLE,
    retrieval_description=(
        "Change an existing calendar event's time, duration, or location — "
        "reschedule it. Matched by part of its current title, same as "
        "cancel_event. Implemented as cancel + recreate, so if the title should "
        "ALSO change, pass `new_title`; otherwise the original title is kept."),
)
async def update_event(title: str, when_iso: str, duration_min: int = 60,
                       location: str = "", new_title: str = "") -> str:
    # No safe executable path exists with the current synced metadata. Do not
    # resolve a title through collapsed cross-source rows or cancel/recreate:
    # either can retire Reminders and discard Calendar details. This guard
    # also covers direct Python callers outside the registry/agent loop.
    return EVENT_UPDATE_UNAVAILABLE


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
    category="calendar_write",
)
async def cancel_event(title: str) -> str:
    q = title.lower().strip()
    # Upcoming FIRST, then the past. Searching only `upcoming` is what made
    # "delete all of my old reminders" impossible: past-due items are by
    # definition not upcoming, so nothing ever matched them and the model was
    # left proposing cancels against the user's FUTURE events instead.
    #
    # MEASURED FAILURE (2026-08-18 22:36 log): asked to delete old reminders,
    # the model called cancel_event six times on upcoming items — the UCSC
    # move-in appointment, the YC event, tomorrow's alarm — because those were
    # the only things the tool could see. The user (correctly) denied all six
    # on the batch confirmation card, and Wisp then reported "the system
    # blocked all of them — they're either already gone or marked as
    # protected", which was not true of any of them. Meanwhile 136 genuinely
    # past-due items sat in the store, unreachable.
    #
    # Upcoming is checked first so an ambiguous title still prefers the live
    # commitment over a stale one with the same name.
    matches = [c for c in assistant_store.upcoming(days=60) if q in c["title"].lower()]
    if not matches:
        matches = [c for c in assistant_store.history(days=365)
                   if q in c["title"].lower()]
    if not matches:
        return f"Nothing upcoming or past matches “{title}”."
    if len(matches) > 1:
        lst = "; ".join(
            f"{c['title']} ({datetime.fromtimestamp(c['when_ts']).strftime('%a %-I:%M %p')})"
            for c in matches[:5])
        return f"Several items match “{title}”: {lst}. Which one? Please be more specific."
    c = matches[0]
    from service.assistant.hub import hub
    await _retire(c)
    await hub.publish({"type": "changed"})
    return f"Cancelled “{c['title']}”."


async def _retire(c: dict) -> None:
    """Remove one commitment everywhere it exists.

    `upcoming`/`history` collapse a commitment that exists under two sources
    into one row (the manual row of a reminder Wisp mirrored into
    Reminders.app, say) — retire every row of that group, or the shadowed twin
    simply becomes visible again and the cancellation looks like it silently
    failed.
    """
    from service.assistant.hub import hub
    group = [c] + [t for t in (assistant_store.get(i) for i in c.get("duplicate_ids") or [])
                   if t]
    for row in group:
        if row["source"] == "calendar" and row.get("source_id"):
            await hub.publish({"type": "delete_calendar_event",
                               "source_id": row["source_id"]})
            assistant_store.set_status(row["id"], "dismissed")
        elif row["source"] == "manual":
            assistant_store.delete(row["id"])
        else:
            # A synced source re-inserts its rows wholesale on the next
            # sync_source, so deleting would only last until then; 'dismissed'
            # is preserved across syncs by design (see the ON CONFLICT clause),
            # and survives the round trip even if the app's own delete races
            # the next sync. Once the upstream item is gone, sync_source prunes
            # the row outright.
            if row["source"] == "reminders" and row.get("source_id"):
                # The app holds the Reminders grant — ask it to remove the real
                # EKReminder, or it keeps its own alarm and notifies anyway.
                await hub.publish({"type": "delete_apple_reminder",
                                   "source_id": row["source_id"]})
            assistant_store.set_status(row["id"], "dismissed")


def past_due_matching(query: str = "", days: int = 365) -> list[dict]:
    """Past-due commitments, newest first, optionally filtered by title.

    Shared by clear_past_reminders and by the confirmation preview the agent
    loop builds for it — the card has to list the SAME items the call will
    actually clear, so both read through here rather than each doing their own
    filtering.
    """
    items = assistant_store.history(days=max(1, min(int(days or 365), 3650)))
    q = (query or "").lower().strip()
    if q:
        items = [c for c in items if q in c["title"].lower()]
    return items


@register(
    "clear_past_reminders",
    "Delete the user's PAST-DUE reminders and events in one go — everything "
    "whose time has already gone by. This is the tool for 'delete my old "
    "reminders', 'clear out my past due stuff', 'get rid of the overdue "
    "ones'. Do NOT call cancel_event over and over for that: it takes one "
    "title at a time and there are often dozens. Pass `query` only to narrow "
    "it to matching titles. Nothing upcoming is ever touched, and the user "
    "sees the full list and confirms before anything is deleted.",
    {"type": "object",
     "properties": {
         "query": {"type": "string",
                   "description": "optional — only clear past items whose "
                                  "title contains this text. Omit to clear "
                                  "all past-due items."},
         "days": {"type": "integer",
                  "description": "how far back to reach, in days (default 365)"},
     },
     "required": []},
    # calendar_write, so it inherits _ALWAYS_CONFIRM_CALENDAR and the
    # never-grantable rule. A bulk irreversible delete is the last thing that
    # should ever be pre-approvable.
    category="calendar_write",
)
async def clear_past_reminders(query: str = "", days: int = 365) -> str:
    items = past_due_matching(query, days)
    if not items:
        return ("Nothing past-due to clear"
                + (f" matching {query!r}" if query else "") + ".")
    from service.assistant.hub import hub
    for c in items:
        await _retire(c)
    await hub.publish({"type": "changed"})
    return (f"Cleared {len(items)} past-due item(s)"
            + (f" matching {query!r}" if query else "") + ".")


_REMINDER_SOURCES = frozenset({"manual", "reminders"})
_REMINDER_SCOPES = frozenset({"today", "tomorrow", "past_due", "upcoming", "all"})


def reminders_matching(scope: str = "all", query: str = "",
                       *, now: float | None = None) -> list[dict]:
    """Active Wisp/Reminders.app items in a deterministic time scope.

    Calendar events are excluded. A Wisp reminder can carry a task-like kind,
    so source provenance—not the display kind—is the safe boundary.
    """
    scope = (scope or "all").strip().lower()
    if scope not in _REMINDER_SCOPES:
        return []
    now = time.time() if now is None else float(now)
    local_now = datetime.fromtimestamp(now)
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    if scope == "today":
        start, end = today.timestamp(), (today + timedelta(days=1)).timestamp()
    elif scope == "tomorrow":
        start = (today + timedelta(days=1)).timestamp()
        end = (today + timedelta(days=2)).timestamp()
    elif scope == "past_due":
        start, end = None, now
    elif scope == "upcoming":
        start, end = now, None
    else:
        start = end = None

    items = assistant_store.active_between(start, end)
    items = [c for c in items
             if c.get("source") in _REMINDER_SOURCES
             or bool(_REMINDER_SOURCES & set(c.get("duplicate_sources") or []))]
    needle = (query or "").strip().lower()
    if needle:
        items = [c for c in items if needle in (c.get("title") or "").lower()]
    return items


@register(
    "search_reminders",
    "Search active Wisp and Apple Reminders records by title, including "
    "overdue reminders. Calendar events are excluded. Use this when the user "
    "asks what a reminder says or when it is due. Storage source labels do "
    "not identify who created the reminder.",
    {"type": "object",
     "properties": {
         "query": {"type": "string", "description": "title text to match"},
         "scope": {"type": "string",
                   "enum": ["today", "tomorrow", "past_due", "upcoming", "all"],
                   "description": "time scope; all includes overdue active reminders"},
     },
     "required": ["query"]},
    category="assistant_read",
    aliases=["when is my vaccine reminder", "find my dentist reminder",
             "what does my reminder say"],
)
async def search_reminders(query: str, scope: str = "all") -> str:
    items = reminders_matching(scope, query)
    if not items:
        return f"Nothing active matches reminder {query!r}."
    lines = []
    for item in items:
        sources = {str(item.get("source") or "")}
        sources.update(str(value) for value in item.get("duplicate_sources") or [])
        storage = "Apple Reminders" if "reminders" in sources else "Wisp"
        when = datetime.fromtimestamp(float(item["when_ts"]))
        lines.append(f"- {item['title']} — {when:%a %b %-d, %Y at %-I:%M %p} [{storage}]")
    return ("Active reminder matches. Bracketed labels identify the storage app, "
            "not the person who created the reminder; creator identity is unknown.\n"
            + "\n".join(lines))


@register(
    "clear_reminders",
    "Delete active Wisp/Reminders.app reminders in one explicit time scope. "
    "Use scope='today' for today's reminders, 'tomorrow' for tomorrow, "
    "'past_due' for overdue reminders, 'upcoming' for future reminders, or "
    "'all' only when the user explicitly asks for every reminder. Calendar "
    "events are excluded. The user sees the exact list and confirms first.",
    {"type": "object",
     "properties": {
         "scope": {"type": "string",
                   "enum": ["today", "tomorrow", "past_due", "upcoming", "all"],
                   "description": "which active reminders to delete"},
         "query": {"type": "string",
                   "description": "optional title text to narrow within the scope"},
         "expected_ids": {"type": "array", "items": {"type": "string"},
                          "description": "optional typed-plan guard: the exact visible reminder ids approved for deletion"},
     },
     "required": ["scope"]},
    category="calendar_write",
    aliases=["delete my reminders for today", "clear all of my reminders",
             "remove tomorrow's reminders", "delete every active reminder"],
)
async def clear_reminders(scope: str, query: str = "",
                          expected_ids: list[str] | None = None) -> str:
    normalized = (scope or "").strip().lower()
    if normalized not in _REMINDER_SCOPES:
        return ("(error: scope must be today, tomorrow, past_due, upcoming, "
                "or all — no reminders were deleted.)")
    items = reminders_matching(normalized, query)
    label = normalized.replace("_", " ")
    if expected_ids is not None:
        expected = {str(value) for value in expected_ids if value}
        actual = {str(item.get("id") or "") for item in items}
        if actual != expected:
            return ("(error: the selected reminder set changed after preview; "
                    "no reminders were deleted. Ask the user to review the updated list.)")
    if not items:
        return (f"No active reminders found for {label}"
                + (f" matching {query!r}" if query else "") + ".")
    from service.assistant.hub import hub
    for c in items:
        await _retire(c)
    await hub.publish({"type": "changed"})
    return (f"Cleared {len(items)} reminder(s) for {label}"
            + (f" matching {query!r}" if query else "") + ".")


@register(
    "join_video_call",
    "Open the video-call link for an upcoming event, matched by part of its "
    "title (or the next event if title is omitted). Reads the URL stored "
    "with the event — does not guess a link.",
    {"type": "object",
     "properties": {"title": {"type": "string", "description": "Part of the event's title. Omit for the next event."}}},
    category="assistant_read",
    aliases=["join my next meeting", "get me into the standup call",
             "join today's video call", "let me into the call"],
)
async def join_video_call(title: str = "") -> str:
    import subprocess

    q = (title or "").strip().lower()
    items = assistant_store.upcoming(days=1) or assistant_store.upcoming(days=7)
    if q:
        items = [c for c in items if q in c["title"].lower()]
    if not items:
        return (f"No upcoming event matches {title!r}." if q
                else "Nothing upcoming in the next 7 days.")
    items = sorted(items, key=lambda c: c["when_ts"])
    for c in items:
        if c.get("url"):
            p = subprocess.run(["open", c["url"]], capture_output=True,
                               text=True, timeout=15)
            if p.returncode != 0:
                return f"(could not open the link for “{c['title']}”: {(p.stderr or '').strip()})"
            return f"Opened the call link for “{c['title']}”."
    named = items[0]["title"]
    return (f"“{named}” doesn't have a call link stored on it. Check the "
            f"invite in Mail or Calendar directly.")
