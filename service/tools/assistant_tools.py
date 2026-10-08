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
from datetime import date, datetime, timedelta, timezone

from service.assistant.store import assistant_store
from service.tools.registry import EVENT_UPDATE_UNAVAILABLE, register

_KIND_LABEL = {"exam": "EXAM", "assignment": "due", "meeting": "meeting",
               "event": "event", "reminder": "reminder"}


def _calendar_local_start(when_iso: str) -> tuple[datetime, datetime]:
    """Validate a proposed local wall time against what EventKit will show."""
    start = datetime.fromisoformat(when_iso)
    from service.tasks.temporal import unambiguous_local_time
    if start.second or start.microsecond:
        raise ValueError("sub-minute Calendar start is not shown in approval")
    if not unambiguous_local_time(start):
        raise ValueError("invalid or ambiguous local time")
    native_local = datetime.fromtimestamp(start.timestamp(), tz=timezone.utc).astimezone()
    if not unambiguous_local_time(native_local.replace(tzinfo=None)):
        raise ValueError("ambiguous local Calendar wall time")
    if start.tzinfo and (start.replace(tzinfo=None) != native_local.replace(tzinfo=None)
                         or start.utcoffset() != native_local.utcoffset()):
        raise ValueError("offset does not match local Calendar time")
    return start, native_local


def calendar_time_problem(when_iso: str) -> str | None:
    try:
        _calendar_local_start(when_iso)
    except (TypeError, ValueError, OverflowError, OSError):
        return ("(error: Calendar start time is invalid, ambiguous, has hidden seconds, "
                "or has an offset that does not match this Mac's local time; nothing changed.)")
    return None


def bind_calendar_local_start(when_iso: str) -> str:
    """Keep an approved wall time tied to this Mac's current UTC offset.

    The offset-bearing value fails validation if the Mac's zone changes while
    an approval card is open, instead of silently saving a different instant.
    """
    _, local_start = _calendar_local_start(when_iso)
    return local_start.isoformat()


def calendar_interval_label(when_iso: str, duration_min: int) -> str:
    """Show both endpoints as the native Calendar will display them."""
    try:
        start, local_start = _calendar_local_start(when_iso)
    except (TypeError, ValueError, OverflowError, OSError):
        return f"invalid or ambiguous local time, offset, or hidden seconds: {when_iso}"
    try:
        minutes = int(duration_min)
        local_end = datetime.fromtimestamp(start.timestamp() + minutes * 60,
                                           tz=timezone.utc).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return f"invalid Calendar duration: {duration_min} min"
    return (f"{local_start:%a %b %-d, %Y %-I:%M %p %Z%z} to "
            f"{local_end:%a %b %-d, %Y %-I:%M %p %Z%z} ({minutes} min)")


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


def _local_day_bounds(now: float) -> tuple[float, float]:
    """Return the current local calendar day as a half-open epoch interval."""
    today = datetime.fromtimestamp(now).date()
    start = datetime.combine(today, datetime.min.time()).timestamp()
    end = datetime.combine(today + timedelta(days=1), datetime.min.time()).timestamp()
    return start, end


def _fmt(c: dict, now: float, *, show_account: bool = False,
         day_start: float | None = None) -> str:
    when = datetime.fromtimestamp(c["when_ts"])
    today = datetime.fromtimestamp(now).date()
    started_before_day = day_start is not None and c["when_ts"] < day_start
    tag, absolute = _day_tag(when.date(), today), when.strftime('%a %b %-d')
    if started_before_day:
        tag = f"TODAY (started {when.strftime('%a %b %-d')})"
    # A week or more out `_day_tag` already IS the absolute date, and printing
    # it twice ("Thu Sep 17 (Thu Sep 17)") is what a delivered schedule read as
    # repeated events.
    day = tag if started_before_day else (
        absolute if tag == absolute else f"{tag} ({absolute})")
    clock = ("all day" if c.get("all_day") else
             f"started {when.strftime('%a %-I:%M %p')}" if started_before_day else
             when.strftime("%-I:%M %p"))
    delta = c["when_ts"] - now
    if started_before_day:
        rel = "continued into today"
    elif c.get("all_day"):
        rel = ""
    elif delta < -300:
        rel = "earlier today"
    elif delta < 300:
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
    relative = f" ({rel})" if rel else ""
    return f"- {day} {clock}{relative} {label}: {c['title']}{who}{loc}{acct}"


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


def _schedule_sources(item: dict) -> set[str]:
    """All sources represented by a (possibly already-collapsed) row."""
    return {item.get("source", "")} | set(item.get("duplicate_sources") or [])


def _is_wisp_only(item: dict) -> bool:
    """A Wisp record with no current Apple Reminders or Calendar copy."""
    sources = _schedule_sources(item)
    return "manual" in sources and not ({"reminders", "calendar"} & sources)


def _without_withheld_sources(items: list[dict], withheld: set[str]) -> list[dict]:
    """Hide rows from sources that could not be verified for this answer.

    A deduped Wisp winner must not keep that source's provenance either, so it
    is presented as Wisp-only. The stored rows remain untouched.
    """
    visible = []
    for item in items:
        if item.get("source") in withheld:
            continue
        if withheld.intersection(item.get("duplicate_sources") or []):
            item = dict(item)
            item["duplicate_sources"] = [source for source in
                                         item.get("duplicate_sources") or []
                                         if source not in withheld]
            item.pop("duplicate_ids", None)
        visible.append(item)
    return visible


def _collapse_schedule_rows(items: list[dict]) -> list[dict]:
    """Defensively collapse mirrored commitments before presenting them.

    The store does this for normal reads. Keeping this small presentation-layer
    guard makes a response resilient while a source sync is in flight (and
    keeps callers with fixture-like stores from relaying a manual/Apple mirror
    pair as two separate things the user has to do).
    """
    by_key: dict[tuple[str, int] | tuple[str, int, int], dict] = {}
    rank = {"manual": 0, "reminders": 1, "calendar": 2}
    for item in items:
        title = " ".join(str(item.get("title") or "").split()).casefold()
        when = item.get("when_ts")
        if not title or when is None:
            key = ("unkeyed", id(item), 0)
        else:
            key = (title, int(float(when) // 60))
        current = by_key.get(key)
        if current is None or rank.get(item.get("source"), 9) < rank.get(current.get("source"), 9):
            by_key[key] = item
    return sorted(by_key.values(), key=lambda item: float(item.get("when_ts") or 0))


def _agenda_day_label(when: datetime, today: date) -> str:
    if when.date() == today:
        return f"Today · {when.strftime('%a %b %-d')}"
    if when.date() == today + timedelta(days=1):
        return f"Tomorrow · {when.strftime('%a %b %-d')}"
    return when.strftime("%A · %b %-d")


def _agenda_item(item: dict, *, display_day_start: float | None = None) -> str:
    when = datetime.fromtimestamp(float(item["when_ts"]))
    started_before_day = (display_day_start is not None
                          and float(item["when_ts"]) < display_day_start
                          and float(item.get("end_ts") or 0) > display_day_start)
    clock = ("All day" if item.get("all_day") else
             f"Started {when.strftime('%a %-I:%M %p')}" if started_before_day else
             when.strftime("%-I:%M %p"))
    location = str(item.get("location") or "").strip()
    suffix = f" @ {location}" if location else ""
    if _is_wisp_only(item):
        suffix += " [Wisp-only; Apple status unverified]"
    continued = " (continued into today)" if started_before_day else ""
    return f"- {clock} — {item.get('title') or 'Untitled'}{suffix}{continued}"


def _format_forward_agenda(items: list[dict], *, now: float, window_label: str,
                           apple_reminders_checked: bool = True,
                           display_day_start: float | None = None) -> str:
    """Render an agenda for a schedule question, not a storage/debug dump."""
    today = datetime.fromtimestamp(now).date()
    days: dict[date, dict[str, list[dict]]] = {}
    for item in items:
        when = datetime.fromtimestamp(float(item["when_ts"]))
        day = when.date()
        if (display_day_start is not None and item["when_ts"] < display_day_start
                and float(item.get("end_ts") or 0) > display_day_start):
            day = datetime.fromtimestamp(display_day_start).date()
        bucket = days.setdefault(day, {"events": [], "reminders": []})
        # A merged row may carry both calendar and reminder provenance. Keep
        # it visible once; each Wisp-only item is clearly marked on its line.
        bucket["events" if "calendar" in _schedule_sources(item) else "reminders"].append(item)

    blocks: list[str] = []
    for day, bucket in days.items():
        heading = _agenda_day_label(datetime.combine(day, datetime.min.time()), today)
        lines = [heading]
        if bucket["events"]:
            lines.append("  Calendar events")
            lines.extend("  " + _agenda_item(item, display_day_start=display_day_start)
                         for item in bucket["events"])
        if bucket["reminders"]:
            lines.append("  Reminders")
            lines.extend("  " + _agenda_item(item, display_day_start=display_day_start)
                         for item in bucket["reminders"])
        blocks.append("\n".join(lines))
    calendar_count = sum("calendar" in _schedule_sources(item) for item in items)
    reminder_count = sum(bool(_schedule_sources(item) & {"manual", "reminders"})
                         for item in items)
    review_note = ""
    if any(_is_wisp_only(item) for item in items):
        review_note = (
            " Wisp-only items have no matching current Apple Reminders item; "
            "some may be older Apple mirrors and others live Wisp reminders. "
            "Keep them unless the user picks one exactly to delete."
            if apple_reminders_checked else
            " Apple Reminders could not be checked, so Wisp reminders are "
            "listed without a confirmed Apple copy; they may still be active.")
    has_native = any("reminders" in _schedule_sources(item) for item in items)
    native_note = (" Apple Reminders deletion status is not independently verified."
                   if has_native else "")
    title = "Schedule" if display_day_start is not None else "Upcoming"
    return (f"{title} — {window_label} ({len(items)} item(s))\n"
            f"Calendar events: {calendar_count}; Wisp/Apple reminders: {reminder_count}. "
            "[Calendar event] and [Reminder] are shown in separate sections."
            + native_note + review_note + "\n\n" + "\n\n".join(blocks))


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
    readiness = await ensure_sources(("calendar",) if calendar_only else
                                     ("calendar", "reminders"))
    # A Reminders round trip that has not posted back yet must not hide a
    # current Calendar answer. Its native rows are withheld (never shown from
    # the previous snapshot) and the answer says so.
    reminders_pending = (not calendar_only and
                         not readiness.get("reminders_fresh", True))
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
    today_start, today_end = _local_day_bounds(now)
    today_period = False
    if period:
        from service.tools.timeranges import resolve_span, BadPeriod
        try:
            start, end, window_label = resolve_span(period)
        except BadPeriod as exc:
            return f"(error: {exc})"
        today_period = start == today_start and end == today_end
        # A named period such as "this month" describes the forward agenda
        # from this moment, not a historical month-to-date dump. Preserve the
        # period's end/boundary semantics while removing elapsed entries.
        forward_start = max(start, now)
        if today_period:
            # A Today view is a day-overlap query: all-day and already-started
            # Calendar events still belong to today. Other sources remain
            # forward-only, matching the existing reminder semantics.
            calendar_rows = assistant_store.calendar_events_overlapping(start, end)
            rows = [row for row in assistant_store.active_between(forward_start, end)
                    if row.get("source") != "calendar"
                    and forward_start <= float(row.get("when_ts") or 0) < end]
            rows = calendar_rows + rows
        else:
            rows = [row for row in assistant_store.active_between(forward_start, end)
                    if forward_start <= float(row.get("when_ts") or 0) < end]
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
    withheld = unavailable_ids | ({"reminders"} if reminders_pending else set())
    items = _without_withheld_sources(items, withheld)
    notice = ("Wisp could not check " + " and ".join(s["label"] for s in unavailable)
              + ". Check its access in Settings; this schedule may be incomplete.\n") if unavailable else ""
    # A pending Reminders sync is secondary to a calendar question, so it trails
    # the answer instead of leading it. A source Wisp could NOT check stays first:
    # that can make the whole schedule incomplete.
    reminder_note = ""
    if reminders_pending and "reminders" not in unavailable_ids:
        reminder_note = ("\nWisp has not received a current Reminders read yet, so Apple "
                         "Reminders items are not shown here. Try again in a moment.")
    # `upcoming()` normally makes this redundant, but the tool must not
    # describe a just-elapsed entry as "upcoming" when a source returns one.
    items = _collapse_schedule_rows(
        [item for item in items
         if float(item.get("when_ts") or 0) >= now
         or (today_period and item.get("source") == "calendar")])
    if not items:
        if notice or reminder_note:
            return (notice + "No scheduled items were found in the sources that could be checked."
                    + reminder_note)
        return f"Today is {today_str}. Nothing scheduled in {window_label}."
    return notice + _format_forward_agenda(
        items, now=now, window_label=window_label,
        apple_reminders_checked="reminders" not in withheld,
        display_day_start=today_start if today_period else None) + reminder_note


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
    clean_title = title.strip()
    if not clean_title:
        return "(error: a reminder title is required; nothing was added.)"
    if not isinstance(kind, str) or kind not in {"reminder", "assignment", "exam", "meeting", "event"}:
        return "(error: unsupported reminder kind; nothing was added.)"
    from service.assistant.outbox import request as app_request
    from service.assistant.hub import hub
    when_str = when.strftime("%a %b %-d at %-I:%M %p")
    payload = {"title": clean_title, "due_ts": ts, "commitment_kind": kind}
    if not hub.has_subscribers:
        # Nothing is sent now, but an earlier attempt at this exact reminder
        # may already exist natively (unknown outcome or verified success). A
        # local twin would alert twice, so fall back only when none can exist.
        # Wisp still notifies at the due time.
        from service.assistant.outbox import reminder_create_fallback_allowed
        if not reminder_create_fallback_allowed(payload):
            return ("(error: the Wisp app is not connected and an earlier Apple Reminders "
                    "write for this reminder is unconfirmed or already exists; nothing was "
                    "added. Reopen Wisp to reconcile it.)")
        return await _add_local_reminder(
            clean_title, ts, kind, when_str, "the Wisp app is not connected")
    result = await app_request("create_reminder", payload)
    if result.get("ok") is not True:
        if result.get("status") == "failed":
            # The app verified the write never happened (for example, no
            # Reminders access). An unknown outcome never falls back: the
            # native item may exist, and a local twin would alert twice.
            return await _add_local_reminder(
                clean_title, ts, kind, when_str,
                str(result.get("error") or "Apple Reminders declined the write"))
        return f"(error: {result.get('error') or 'Reminders creation was not verified'}; nothing was confirmed.)"
    await hub.publish({"type": "changed"})
    return f"Reminder set: “{clean_title}” — {when_str} in Apple Reminders and Wisp."


async def _add_local_reminder(title: str, ts: float, kind: str, when_str: str,
                              reason: str) -> str:
    from service.assistant.hub import hub
    c = assistant_store.add_manual(title, ts, kind=kind)
    try:
        await hub.publish({"type": "changed"})
    except Exception:  # noqa: BLE001
        pass
    return (f"Reminder set: “{c['title']}” — {when_str} in Wisp only; "
            f"Apple Reminders was not changed ({reason.rstrip('.')}).")


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
    reminder_rows = [row for row in group if row.get("source") == "reminders"]
    if not reminder_rows:
        # No native twin exists (a pre-A18 or Wisp-only reminder), so a local
        # move changes nothing outside Wisp and needs no native receipt.
        manual_ids = [row["id"] for row in group if row.get("source") == "manual"]
        if not manual_ids:
            return "(error: this item is not a Wisp or Apple reminder; nothing was changed.)"
        if assistant_store.update_schedule(manual_ids, new_when, final_title) < 1:
            return "(error: the reminder changed before it could be updated; try again.)"
        from service.assistant.hub import hub
        await hub.publish({"type": "changed"})
        return f"Reminder updated: “{final_title}” — {target:%a %b %-d at %-I:%M %p} in Wisp."
    if len(reminder_rows) > 1:
        return "(error: several native reminders share this item; select an exact reminder before changing it.)"
    if any(not row.get("source_id") for row in reminder_rows):
        return "(error: exact Reminders identity is unavailable; nothing was changed.)"
    from service.assistant.outbox import request as app_request
    from service.assistant.hub import hub
    for row in reminder_rows:
        result = await app_request("update_reminder", {
            "source_id": row["source_id"], "expected_title": row["title"],
            "expected_due_ts": row["when_ts"], "title": final_title, "due_ts": new_when,
        })
        if result.get("ok") is not True:
            return f"(error: {result.get('error') or 'Reminder update was not verified'}; check the exact reminder before another attempt.)"
    manual_ids = [row["id"] for row in group if row["source"] == "manual"]
    if manual_ids:
        assistant_store.update_schedule(manual_ids, new_when, final_title)
    await hub.publish({"type": "changed"})

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
                      "description": "local START datetime, e.g. 2026-07-14T15:00; for 6–7 PM use 18:00"},
         "duration_min": {"type": "integer", "description": "event length in minutes; for 6–7 PM use 60 (default 60)"},
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
    if problem := calendar_time_problem(when_iso):
        return problem
    when, _ = _calendar_local_start(when_iso)
    if when.timestamp() < time.time() - 60:
        return f"({when_iso} is in the past — not added)"
    if (not isinstance(title, str) or not title.strip()
            or type(duration_min) is not int or not 1 <= duration_min <= 10080):
        return "(error: a title and duration of 1–10080 minutes are required; nothing changed.)"
    interval = calendar_interval_label(when_iso, duration_min)
    from service.assistant.outbox import request as app_request
    result = await app_request("create_calendar_event", {
        "title": title.strip(), "when_ts": when.timestamp(),
        "duration_min": duration_min, "location": location or "",
    })
    if not result.get("ok"):
        return f"(error: {result.get('error') or 'Calendar creation was not confirmed'}.)"
    return f"Added “{title}” to your calendar for {interval}."



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
    error = await _retire(c)
    if error:
        return f"(error: {error})"
    await hub.publish({"type": "changed"})
    return f"Cancelled “{c['title']}”."


async def _retire(c: dict) -> str | None:
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
    if sum(row["source"] in {"calendar", "reminders"} for row in group) > 1:
        return "Several native records share this item; select one exact record before cancelling."
    # Calendar writes finish first. A failure cannot retire the local twin or
    # claim the whole group was cancelled. Native receipts reconcile Calendar.
    from service.assistant.outbox import request as app_request
    for row in group:
        if row["source"] == "calendar":
            if not row.get("source_id") or row.get("when_ts") is None:
                return "Calendar identity is incomplete; cancellation was not confirmed."
            result = await app_request("delete_calendar_event", {
                "source_id": row["source_id"], "when_ts": row["when_ts"]})
            if not result.get("ok"):
                return str(result.get("error") or "Calendar cancellation was not confirmed")
        elif row["source"] == "reminders":
            if not row.get("source_id") or row.get("when_ts") is None:
                return "Reminders identity is incomplete; cancellation was not confirmed."
            result = await app_request("delete_reminder", {
                "source_id": row["source_id"], "expected_title": row["title"],
                "expected_due_ts": row["when_ts"]})
            if result.get("ok") is not True:
                return str(result.get("error") or "Reminders cancellation was not confirmed")
    for row in group:
        if row["source"] == "calendar":
            continue
        elif row["source"] == "manual":
            assistant_store.delete(row["id"])
        else:
            # A synced source re-inserts its rows wholesale on the next
            # sync_source, so deleting would only last until then; 'dismissed'
            # is preserved across syncs by design (see the ON CONFLICT clause),
            # and survives the round trip even if the app's own delete races
            # the next sync. Once the upstream item is gone, sync_source prunes
            # the row outright.
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
        error = await _retire(c)
        if error:
            return f"(error: stopped before completing all removals: {error})"
    await hub.publish({"type": "changed"})
    return (f"Cleared {len(items)} past-due item(s)"
            + (f" matching {query!r}" if query else "") + ".")


_REMINDER_SOURCES = frozenset({"manual", "reminders"})
# A Wisp-only record has no Apple copy to reconcile against, so deleting the
# reminder in Reminders.app never retires it. Once it is this far overdue it is
# kept (clear_reminders / clear_past_reminders still see it) but no longer
# listed as a current reminder by a read.
STALE_WISP_ONLY_DAYS = 14
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


def _drop_stale_wisp_only(items: list[dict], now: float | None = None
                          ) -> tuple[list[dict], int]:
    """Split off long-overdue Wisp-only records; return (kept, hidden count)."""
    cutoff = (time.time() if now is None else float(now)) - STALE_WISP_ONLY_DAYS * 86400
    kept = []
    for item in items:
        sources = {str(item.get("source") or "")} | set(item.get("duplicate_sources") or [])
        wisp_only = "manual" in sources and not ({"reminders", "calendar"} & sources)
        if wisp_only and float(item.get("when_ts") or 0) < cutoff:
            continue
        kept.append(item)
    return kept, len(items) - len(kept)


_UNDATED_NOT_INCLUDED = ("Reminders without a due date were not included in the latest read, "
                         "so none are listed or verified.")


def _undated_report(query: str, scope: str) -> tuple[str, str]:
    """(section, caveat) for the incomplete Apple reminders that have no due date.

    The stored list is only believed when it was written from the very native snapshot
    the Reminders receipt records as the latest one. Otherwise (never received, or a
    later read left it behind) nothing from it is listed and the caveat says so. A list
    the app had to cut at its cap is reported as "k of N", never as exhaustive. They belong
    to no time window, so only scope 'all' lists them; a narrower scope counts them.
    """
    view, rows = assistant_store.undated_listing(query)
    if view["freshness"] != "current":
        return "", _UNDATED_NOT_INCLUDED
    truncated = view["status"] == "truncated"
    stored, total = view["stored"], view["total"]
    plural = lambda n: "s" if n != 1 else ""  # noqa: E731
    if scope == "all":
        if rows:
            lines = [f"- {item['title']}" + (f" ({item['context']})" if item.get("context") else "")
                     for item in rows]
            header = ("Apple Reminders with no due date (incomplete)"
                      + (f", showing {stored} of {total}; the rest were not synced:" if truncated else ":"))
            return header + "\n" + "\n".join(lines), ""
        if truncated:
            return "", (f"No match among the {stored} of {total} reminders with no due date that "
                        f"were synced; the other {total - stored} were not checked.")
        return "", ""
    if not truncated:
        count = len(rows)
        if not count:
            return "", ""
        return (f"{count} incomplete Apple reminder{plural(count)} with no due date "
                f"{'are' if count != 1 else 'is'} not in this time window; ask for all reminders to see "
                f"{'them' if count != 1 else 'it'}."), ""
    if not (query or "").strip():
        return (f"{total} incomplete Apple reminder{plural(total)} with no due date "
                f"{'are' if total != 1 else 'is'} not in this time window; ask for all reminders to see "
                f"{'them' if total != 1 else 'it'}."), ""
    return (f"{len(rows)} of the {stored} synced reminders with no due date match (of {total} in all); "
            "they are not in this time window; ask for all reminders to see them."), ""


def _stale_note(hidden: int) -> str:
    if not hidden:
        return ""
    return (f"\n{hidden} older Wisp-only record{'s' if hidden != 1 else ''} overdue by more "
            f"than {STALE_WISP_ONLY_DAYS} days {'are' if hidden != 1 else 'is'} not listed "
            "(no Apple Reminders copy is confirmed). Ask to clear past-due items to remove them.")


@register(
    "search_reminders",
    "Search current Apple Reminders and retained Wisp-only reminder records "
    "by title, including overdue items. Calendar events are excluded. A "
    "Wisp-only record may be a historical mirror whose Apple copy was deleted; "
    "show it separately for review, never call it a current Apple item. "
    "Storage source labels do not identify who created a reminder.",
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
    from service.assistant.sync_status import ensure_sources
    readiness = await ensure_sources(("reminders",), timeout_seconds=4.0)
    state = next((source["state"] for source in readiness["sources"]
                  if source["id"] == "reminders"), "unavailable")
    if state == "unavailable":
        notice = ("Wisp could not check Apple Reminders; check its access in "
                  "Settings. No Apple Reminders item is listed or verified.")
    elif not readiness.get("reminders_fresh", True):
        notice = ("I haven't received a current Reminders read yet, so no Apple "
                  "Reminders item is listed or verified. Try again in a moment.")
    elif state != "ready":
        notice = ("Apple Reminders is still syncing or waiting for access, so no "
                  "Apple Reminders item is listed or verified yet. Try again in a moment.")
    else:
        notice = ""
    # Wisp's own records remain searchable while Apple Reminders cannot be
    # verified; only the native snapshot is withheld.
    items, hidden = _drop_stale_wisp_only(reminders_matching(scope, query))
    stale_note = _stale_note(hidden)
    if notice:
        items = _without_withheld_sources(items, {"reminders"})
        wisp_lines = []
        for item in items:
            when = datetime.fromtimestamp(float(item["when_ts"]))
            wisp_lines.append(f"- {item['title']} — {when:%a %b %-d, %Y at %-I:%M %p}")
        if not wisp_lines:
            return notice + f"\nNo Wisp-only reminder record matches {query!r}." + stale_note
        return (notice + "\nWisp-only records (Apple copy not confirmed because "
                "Apple Reminders couldn't be checked; they may still be active):\n"
                + "\n".join(wisp_lines) + stale_note)
    undated_section, undated_caveat = _undated_report(query, scope)
    if not items and not undated_section:
        return (f"A current Reminders read found no active match for {query!r}"
                + (" among reminders with a due date" if undated_caveat else "") + ". "
                "An older remembered item does not establish a current reminder."
                + (("\n" + undated_caveat) if undated_caveat else "") + stale_note)
    native_lines, wisp_lines = [], []
    for item in items:
        sources = {str(item.get("source") or "")}
        sources.update(str(value) for value in item.get("duplicate_sources") or [])
        when = datetime.fromtimestamp(float(item["when_ts"]))
        line = f"- {item['title']} — {when:%a %b %-d, %Y at %-I:%M %p}"
        (native_lines if "reminders" in sources else wisp_lines).append(line)
    sections = ["A fresh Apple Reminders read supersedes older remembered claims. "
                "These lists describe storage, not who created an item. "
                "Recently Deleted status is not independently verified."]
    sections.append("Apple Reminders incomplete-item matches:\n" +
                    ("\n".join(native_lines) if native_lines else
                     "None with a due date." if undated_section else "None."))
    if undated_section:
        sections.append(undated_section)
    if wisp_lines:
        sections.append("Wisp-only records, kept for review: these are not "
                        "verified active Apple Reminders items. Some may be older "
                        "Apple mirrors and others live Wisp reminders. Do not "
                        "delete them without exact user selection.\n"
                        + "\n".join(wisp_lines))
    if undated_caveat:
        sections.append(undated_caveat)
    return "\n".join(sections) + stale_note


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
        error = await _retire(c)
        if error:
            return f"(error: stopped before completing all removals: {error})"
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
