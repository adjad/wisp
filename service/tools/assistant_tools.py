"""Agent tools over the assistant commitments store.

These make schedule questions ("what's on my calendar today?", "what's due this
week?") answerable from the local store in milliseconds — instead of the agent
improvising AppleScript against Calendar.app, which is slow, permission-fragile
(the exact failure the user hit), and unaware of manual reminders. The store is
fed by the app's CalendarReader (EventKit) plus manual additions.
"""
from __future__ import annotations

from service.assistant.verified_reminders import expected_date_fields

import re
import contextlib
import contextvars
import time
from datetime import date, datetime, timedelta, timezone

from service.assistant.store import assistant_store
from service.tools.registry import EVENT_UPDATE_UNAVAILABLE, register

_KIND_LABEL = {"exam": "EXAM", "assignment": "due", "meeting": "meeting",
               "event": "event", "reminder": "reminder"}


def _access_fixes(unavailable: list[dict]) -> str:
    """Where to allow each unreadable source, named exactly, as sentences that
    end in a space. Each one is also noted for the Ask reply's fix button
    (access_needed), so the button never depends on the model's wording. A
    source that failed for another reason gets the general pointer."""
    from service.tools import access_needed
    fixes = [access_needed.need(row["id"], row.get("reason_kind") or "") for row in unavailable]
    named = list(dict.fromkeys(fix for fix in fixes if fix))
    if len(named) < len(fixes):
        named.append("Check its access in Settings.")
    return " ".join(named) + " " if named else ""


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


def _calendar_occurrence_key(item: dict) -> tuple | None:
    """Native occurrence identity, never a title/time similarity guess."""
    import math
    native_id, when = item.get("source_id"), item.get("when_ts")
    if (not isinstance(native_id, str) or not native_id.strip()
            or type(when) not in (int, float) or not math.isfinite(when)):
        return None
    account, context = item.get("account"), item.get("context")
    if any(value is not None and not isinstance(value, str) for value in (account, context)):
        return None
    return (native_id, when, account, context)


def _collapse_schedule_rows(items: list[dict]) -> list[dict]:
    """Keep native Calendar occurrences separate from manual/Reminder mirrors.

    Missing native identity and conflicting descriptions remain separate. The
    same occurrence can arrive through both the raw and normal store reads;
    an absent field on one read is not a contradictory field value.
    """
    by_key: dict[tuple, dict] = {}
    calendar: dict[tuple, list[int]] = {}
    calendar_rows: list[dict] = []
    rank = {"manual": 0, "reminders": 1, "calendar": 2}
    for item in items:
        if item.get("source") == "calendar":
            identity = _calendar_occurrence_key(item)
            peers = calendar.setdefault(identity, []) if identity is not None else []
            fields = ("title", "kind", "end_ts", "all_day", "location", "organizer", "url", "status")
            compatible = [index for index in peers
                          if all(item.get(field) == calendar_rows[index].get(field)
                                 for field in fields
                                 if item.get(field) is not None
                                 and calendar_rows[index].get(field) is not None)]
            if len(compatible) == 1:
                index = compatible[0]
                peer = calendar_rows[index]
                additions = {field: item[field] for field in fields
                             if peer.get(field) is None and item.get(field) is not None}
                if additions:
                    calendar_rows[index] = {**peer, **additions}
                continue
            # More than one compatible description is ambiguous, not proof
            # that this row denotes either of those conflicting events.
            peers.append(len(calendar_rows))
            calendar_rows.append(item)
            continue
        title = " ".join(str(item.get("title") or "").split()).casefold()
        when = item.get("when_ts")
        if not title or when is None:
            key = ("unkeyed", id(item), 0)
        else:
            key = (title, int(float(when) // 60), bool(item.get('all_day')))
        current = by_key.get(key)
        if current is None or rank.get(item.get("source"), 9) < rank.get(current.get("source"), 9):
            by_key[key] = item
    return sorted(calendar_rows + list(by_key.values()),
                  key=lambda item: float(item.get("when_ts") or 0))


def _agenda_day_label(when: datetime, today: date) -> str:
    if when.date() == today:
        return f"Today · {when.strftime('%a, %b %-d')}"
    if when.date() == today + timedelta(days=1):
        return f"Tomorrow · {when.strftime('%a, %b %-d')}"
    return when.strftime("%A · %b %-d")


# `*` and backtick open Markdown runs in the chat renderer, and titles and
# locations are written by other people (see brief._clean).
_AGENDA_MD_RUN = re.compile(r"[*`]+")


def _agenda_text(value: object) -> str:
    return _AGENDA_MD_RUN.sub("", " ".join(str(value or "").split())).strip()


def _agenda_clock(when: datetime, now: float) -> str:
    """A wall-clock time; the zone appears only when it differs from now's.

    Every row is already local time, so "PDT-0700" on each line was noise.
    Across a DST change it is a fact (it also separates the two 1:30 AMs of a
    fall-back night), so it is kept exactly there.
    """
    current = datetime.fromtimestamp(now, tz=timezone.utc).astimezone().utcoffset()
    clock = when.strftime("%-I:%M %p")
    return clock if when.utcoffset() == current else f"{clock} {when.strftime('%Z')}"


def _agenda_item(item: dict, *, now: float | None = None,
                 display_day_start: float | None = None) -> str:
    now = time.time() if now is None else now
    when = datetime.fromtimestamp(float(item["when_ts"]), tz=timezone.utc).astimezone()
    started_before_day = (display_day_start is not None
                          and float(item["when_ts"]) < display_day_start
                          and float(item.get("end_ts") or 0) > display_day_start)
    clock = ("All day" if item.get("all_day") else
             f"Started {when.strftime('%a')} {_agenda_clock(when, now)}" if started_before_day else
             _agenda_clock(when, now))
    location = _agenda_text(item.get("location"))
    suffix = f" · {location}" if location else ""
    if _is_wisp_only(item):
        suffix += " [Wisp-only; Apple status unverified]"
    continued = ""
    if started_before_day:
        display_date = datetime.fromtimestamp(display_day_start).date()
        label = "today" if display_date == datetime.fromtimestamp(time.time()).date() else display_date.isoformat()
        continued = f" (continued into {label})"
    return f"- **{clock}** — {_agenda_text(item.get('title')) or 'Untitled'}{suffix}{continued}"


def _agenda_count(n: int, noun: str) -> str:
    return f"no {noun}s" if not n else f"{n} {noun}" + ("" if n == 1 else "s")


def _format_forward_agenda(items: list[dict], *, now: float, window_label: str,
                           apple_reminders_checked: bool = True,
                           display_day_start: float | None = None,
                           reminders_read: bool = True) -> str:
    """Render an agenda the user can read as-is.

    Deterministic calendar reads (workflows.reads.execute_read) show this text
    with no model pass, so it must not carry model-facing scaffolding: the old
    "Upcoming — this week (9 item(s))" / "[Calendar event] and [Reminder] are
    shown in separate sections." header reached the user verbatim. The summary
    line still states both counts in words ("9 events, no reminders"), which
    router._edit_reference_subset reads to scope a follow-up edit, and every
    day keeps calendar events and reminders apart so neither reader takes an
    event for a reminder. "no reminders" is said only when `reminders_read`:
    a calendar-only or withheld Reminders read is not evidence of none.
    """
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
        lines = [f"**{heading}**"]
        lines.extend(_agenda_item(item, now=now, display_day_start=display_day_start)
                     for item in bucket["events"])
        if bucket["reminders"]:
            if bucket["events"]:
                lines.append("*Reminders*")
            lines.extend(_agenda_item(item, now=now, display_day_start=display_day_start)
                         for item in bucket["reminders"])
        blocks.append("\n".join(lines))
    calendar_count = sum("calendar" in _schedule_sources(item) for item in items)
    reminder_count = sum(bool(_schedule_sources(item) & {"manual", "reminders"})
                         for item in items)
    counts = [_agenda_count(calendar_count, "event")]
    if reminder_count or reminders_read:
        counts.append(_agenda_count(reminder_count, "reminder"))
    notes = []
    if any(_is_wisp_only(item) for item in items):
        notes.append(
            "Wisp-only items have no matching current Apple Reminders item; "
            "some may be older Apple mirrors and others live Wisp reminders. "
            "Keep them unless the user picks one exactly to delete."
            if apple_reminders_checked else
            "Apple Reminders could not be checked, so Wisp reminders are "
            "listed without a confirmed Apple copy; they may still be active.")
    if any("reminders" in _schedule_sources(item) for item in items):
        notes.append("Apple Reminders deletion status is not independently verified.")
    window = re.sub(r"\bnext 1 day\(s\)", "next day", window_label)
    window = window.replace("day(s)", "days")
    header = f"{window[:1].upper()}{window[1:]}: {', '.join(counts)}"
    return (header + "\n\n" + "\n\n".join(blocks)
            + ("\n\n" + " ".join(notes) if notes else ""))


def _match_schedule_query(rows: list[dict], query: str) -> list[dict]:
    """Bounded assessment synonyms; ordinary event queries retain literal matching.

    Never choose the first of several assessments. Return all candidates with
    their original identity/time so the caller can clarify the intended event.
    """
    from difflib import SequenceMatcher
    normalize = lambda value: re.sub(r"[\W_]+", "", value.casefold())
    ordinals = {"one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7","eight":"8","nine":"9","ten":"10","eleven":"11","twelve":"12"}
    tokenize = lambda text: [ordinals.get(w,w) for w in re.findall(r"[a-z]+|[0-9]+",text.casefold())]
    words = tokenize(query)
    types = {"test", "exam", "examination", "assessment", "midterm", "final", "quiz"}
    requested = types & set(words)
    if not requested:
        return [row for row in rows if normalize(query) in normalize(str(row.get("title") or ""))]
    ignored = {"when", "what", "time", "day", "is", "are", "my", "the", "next", "upcoming", "a", "an", "of", "for"}
    subjects = [w for w in words if w not in types | ignored]
    specific = requested & {"midterm", "final", "quiz"}
    allowed = specific or (types if "assessment" in requested else types - {"quiz"})
    results = []
    for row in rows:
        title = str(row.get("title") or "")
        tokens = tokenize(title)
        if not allowed & set(tokens):
            continue
        # A study session or review is not the assessment itself.
        if {"practice", "prep", "preparation", "review", "study"} & set(tokens) and not set(subjects) & {"practice", "prep", "preparation", "review", "study"}:
            continue
        def matches(subject):
            return subject in tokens or (len(subject) >= 4 and any(
                len(token) >= 4 and SequenceMatcher(None, subject, token).ratio() >= .84
                for token in tokens if token not in types))
        if all(matches(subject) for subject in subjects):
            results.append(row)
    return results


def _format_calendar_end_times(items: list[dict], *, now: float, window_label: str) -> str:
    """Scheduled end times the user can read as-is, in the agenda's style.

    Like _format_forward_agenda, deterministic reads show this with no model
    pass, so the old "Scheduled Calendar end times (...); these do not verify
    actual completion." / "Multiple matching events; ..." lines and the
    "%Y-%m-%d %-I:%M %p %Z%z" stamps reached the user verbatim. The summary
    line keeps the agenda's "<window>: N events" shape, which
    router._edit_reference_subset reads to scope a follow-up edit. Each row
    still names its start so two same-titled events stay distinct, and the
    caveat is one plain sentence: a calendar end is not proof it finished.
    """
    today = datetime.fromtimestamp(now).date()
    lines = []
    for item in items:
        title = _agenda_text(item.get("title")) or "Untitled"
        start = datetime.fromtimestamp(float(item["when_ts"]), tz=timezone.utc).astimezone()
        start_day = _agenda_day_label(start, today)
        end_ts = item.get("end_ts")
        if item.get("all_day"):
            starts = f"{start_day}, all day"
        else:
            starts = f"{start_day}, {_agenda_clock(start, now)}"
        if end_ts is None or float(end_ts) < float(item["when_ts"]):
            ends = "has no end time on the calendar"
        elif item.get("all_day"):
            # The stored end is the exclusive midnight after the last day.
            last = datetime.fromtimestamp(max(float(end_ts) - 1, float(item["when_ts"]))).date()
            ends = "is all day, with no set finish time"
            if last != start.date():
                ends += f"; it runs through **{last.strftime('%a, %b %-d')}**"
        else:
            end = datetime.fromtimestamp(float(end_ts), tz=timezone.utc).astimezone()
            clock = _agenda_clock(end, now)
            ends = (f"ends at **{clock}**" if end.date() == start.date() else
                    f"ends **{_agenda_day_label(end, today)}, {clock}**")
        lines.append(f"- {title} ({starts}) {ends}")
    window = re.sub(r"\bnext 1 day\(s\)", "next day", window_label).replace("day(s)", "days")
    header = f"{window[:1].upper()}{window[1:]}: {_agenda_count(len(items), 'event')}"
    caveat = ("These are scheduled end times from your calendar, not a record of "
              "when anything actually ended.")
    return header + "\n\n" + "\n".join(lines) + "\n\n" + caveat


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
         "boundary": {"type": "string", "enum": ["start", "end"], "description": "Named Calendar time question: end returns scheduled end timestamps, never actual completion. Requires calendar_only and query. Default start preserves the agenda."},
         "query": {"type": "string", "description": "Filter titles within the requested range; bounded subject typos and test/exam synonyms are supported. Multiple matches remain distinct; ask which instead of selecting one."},
         "account": {"type": "string",
                     "description": "only include this linked calendar account (only useful when more than one is linked)"},
         "include_holidays": {"type": "boolean",
                     "description": "include subscribed holiday calendars only when the user explicitly asks for holidays; default false"},
     }},
    category="assistant_read",
)
async def get_upcoming(days: int = 7, account: str | None = None,
                       include_holidays: bool = False, period: str = "",
                       calendar_only: bool = False, query: str = "",
                       boundary: str = "start") -> str:
    if boundary not in {"start", "end"} or (boundary == "end" and (not calendar_only or not query)):
        return "(error: scheduled end projection requires a named Calendar-only query)"
    from service.assistant.sync_status import ensure_sources
    readiness = await ensure_sources(("calendar",) if calendar_only else
                                     ("calendar", "reminders"))
    if boundary == "end":
        calendar_sources = [source for source in readiness.get('sources', ())
                            if source.get('id') == 'calendar']
        if not calendar_sources or any(source.get('state') != 'ready' for source in calendar_sources):
            return "Wisp could not verify Calendar readiness; no scheduled end time has been checked."
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
            start, end, window_label = resolve_span(period, forward=True)
        except BadPeriod as exc:
            return f"(error: {exc})"
        today_period = start == today_start and end == today_end
        # A named period such as "this month" describes the forward agenda
        # from this moment, not a historical month-to-date dump. Preserve the
        # period's end/boundary semantics while removing elapsed entries.
        forward_start = max(start, now)
        # Calendar appointments can begin before a requested range and still
        # occupy it. Today includes the entire day; other explicit periods
        # retain ongoing and future appointments without elapsed history.
        calendar_rows = assistant_store.calendar_events_overlapping(
            start if today_period else forward_start, end)
        rows = [row for row in assistant_store.active_between(forward_start, end)
                if row.get("source") != "calendar"
                and forward_start <= float(row.get("when_ts") or 0) < end]
    else:
        import math
        # upcoming() collapses by title/minute before account filtering. Read
        # raw Calendar occurrences instead, retaining the existing inclusive
        # elapsed-seconds horizon (explicit periods remain half-open).
        end = now + days * 86400
        calendar_rows = assistant_store.calendar_events_overlapping(
            now, math.nextafter(end, math.inf))
        rows = assistant_store.upcoming(now=now, days=days)
    # Calendar comes from exactly one raw read. The independent legacy read
    # above contributes other sources only, never a later/deleted occurrence
    # or Calendar provenance attached to a manual display-dedupe winner.
    rows = calendar_rows + _without_withheld_sources(rows, {"calendar"})
    items = _filter_account(rows, account)
    if calendar_only:
        # Raw occurrences are available above. A manual winner carrying
        # Calendar display-dedupe provenance is not another native event.
        items = [row for row in items if row.get("source") == "calendar"]
    if query:
        items = _match_schedule_query(items, query)
    items = _without_holiday_calendars(items, include_holidays=bool(include_holidays))
    unavailable = [s for s in readiness["sources"] if s["state"] == "unavailable"]
    unavailable_ids = {s["id"] for s in unavailable}
    withheld = unavailable_ids | ({"reminders"} if reminders_pending else set())
    items = _without_withheld_sources(items, withheld)
    notice = ("Wisp could not check " + " and ".join(s["label"] for s in unavailable) + ". "
              + _access_fixes(unavailable) + "This schedule may be incomplete.\n") if unavailable else ""
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
         or ((period or boundary == "end") and item.get("source") == "calendar")])
    if not items:
        if notice or reminder_note:
            return (notice + "No scheduled items were found in the sources that could be checked."
                    + reminder_note)
        return f"Today is {today_str}. Nothing scheduled in {window_label}."
    if boundary == "end":
        return notice + _format_calendar_end_times(items, now=now, window_label=window_label)
    return notice + _format_forward_agenda(
        items, now=now, window_label=window_label,
        apple_reminders_checked="reminders" not in withheld,
        reminders_read=not calendar_only and "reminders" not in withheld,
        display_day_start=max(start, today_start) if period else None) + reminder_note


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
    unavailable = [s for s in readiness["sources"] if s["state"] == "unavailable"]
    if unavailable:
        return "Wisp could not check Calendar. " + _access_fixes(unavailable) + "Then try again."
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


def _reminder_due_time(when_iso: str) -> tuple[float, str]:
    """Resolve one reminder instant before either native or local effects.

    Naive values name Mac-local wall times and must identify exactly one
    instant. An explicit ISO offset already identifies an instant; the native
    reminder bridge receives its epoch, not Calendar-style local components.
    """
    from math import isfinite
    from service.tasks.temporal import unambiguous_local_time

    # Preserve datetime.fromisoformat's clock formats (including a space
    # separator), while refusing every date-only form it accepts as midnight.
    try:
        date.fromisoformat(when_iso)
    except (TypeError, ValueError):
        pass
    else:
        raise ValueError("timed reminders require an explicit clock; use all_day for a date")
    when = datetime.fromisoformat(when_iso)
    if when.tzinfo is None and not unambiguous_local_time(when):
        raise ValueError("invalid or ambiguous local reminder time")
    ts = when.timestamp()
    if not isfinite(ts):
        raise ValueError("non-finite reminder time")
    if when.tzinfo is not None:
        local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        label = local.strftime("%a %b %-d at %-I:%M %p %Z%z")
    else:
        label = when.strftime("%a %b %-d at %-I:%M %p")
    return ts, label


@register(
    "add_reminder",
    "Create a reminder in Wisp's local commitment store. Wisp will notify the "
    "user at the right time and show a countdown. Use when the user asks to be "
    "reminded of something, asks for an alarm/nudge, or to track a deadline. "
    "This creates a Wisp notification and requests an Apple Reminders mirror, "
    "not a ringing Clock alarm. A calendar appointment or saved memory is not "
    "a reminder. Use all_day=true with a YYYY-MM-DD date for an explicitly "
    "all-day reminder; it has no timed alarm. Otherwise ask for an unspecified alert time.",
    {"type": "object",
     "properties": {
         "title": {"type": "string", "description": "what to remind about"},
         "when_iso": {"type": "string",
                      "description": "local datetime ISO format, e.g. 2026-07-13T16:00"},
         "kind": {"type": "string",
                  "enum": ["reminder", "assignment", "exam", "meeting", "event"],
                  "description": "defaults to reminder"},
         "all_day": {"type": "boolean", "description": "Explicit date-only reminder; when_iso must be YYYY-MM-DD. No clock alert."},
     },
     "required": ["title", "when_iso"]},
    category="assistant_write",
)
async def add_reminder(title: str, when_iso: str, kind: str = "reminder", all_day: bool = False) -> str:
    from service.assistant.verified_reminders import valid_due_date, due_timestamp
    if type(all_day) is not bool or (all_day and not valid_due_date(when_iso)):
        return '(error: all-day reminders require an explicit YYYY-MM-DD date; nothing was added.)'
    try:
        if all_day:
            ts, when_str = due_timestamp({'all_day': True, 'due_date': when_iso}), ''
        else:
            ts, when_str = _reminder_due_time(when_iso)
    except (TypeError, ValueError, OverflowError, OSError):
        return (f"(bad when_iso {when_iso!r} — use a valid, unambiguous local ISO "
                "time, e.g. 2026-07-13T16:00, or an explicit UTC offset; nothing was added.)")
    if ((datetime.fromisoformat(when_iso).date() < datetime.now().date()) if all_day
            else ts < time.time() - 60):
        return f"({when_iso} is in the past — not added)"
    clean_title = title.strip() if isinstance(title, str) else ''
    if not clean_title:
        return "(error: a reminder title is required; nothing was added.)"
    if not isinstance(kind, str) or kind not in {"reminder", "assignment", "exam", "meeting", "event"}:
        return "(error: unsupported reminder kind; nothing was added.)"
    from service.assistant.outbox import app_connected, request as app_request
    from service.assistant.hub import hub
    if all_day:
        when_str = datetime.fromisoformat(when_iso).strftime('%a %b %-d, %Y') + ' (all day; no timed alarm)'
    payload = {"title": clean_title, "commitment_kind": kind,
               **({'all_day': True, 'due_date': when_iso} if all_day else {'due_ts': ts})}
    if not await app_connected(via=hub):
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
            clean_title, ts, kind, when_str, "the Wisp app is not connected",
            **({'all_day': True, 'due_date': when_iso} if all_day else {}))
    result = await app_request("create_reminder", payload)
    if result.get("ok") is not True:
        if result.get("status") == "failed":
            # The app verified the write never happened (for example, no
            # Reminders access). An unknown outcome never falls back: the
            # native item may exist, and a local twin would alert twice.
            return await _add_local_reminder(
                clean_title, ts, kind, when_str,
                str(result.get("error") or "Apple Reminders declined the write"),
                **({'all_day': True, 'due_date': when_iso} if all_day else {}))
        return f"(error: {result.get('error') or 'Reminders creation was not verified'}; nothing was confirmed.)"
    if all_day:
        from service.assistant.verified_reminders import validate_result
        try:
            validate_result('create_reminder', payload, result)
        except (KeyError, TypeError, ValueError):
            return '(error: the all-day reminder date was not verified; check Reminders before retrying.)'
    await hub.publish({"type": "changed"})
    return f"Reminder set: “{clean_title}” — {when_str} in Apple Reminders and Wisp."


async def _add_local_reminder(title: str, ts: float, kind: str, when_str: str,
                              reason: str, *, all_day: bool = False, due_date: str | None = None) -> str:
    from service.assistant.hub import hub
    c = assistant_store.add_manual(title, ts, kind=kind, **({'all_day': True, 'due_date': due_date} if all_day else {}))
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

    # Rescheduling a hidden overdue reminder by name is allowed (it un-hides it by moving
    # its due time); a list or search never shows it.
    candidates = reminders_matching("all", title, include_stale=True)
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

    from service.assistant.verified_reminders import valid_due_date, validate_result
    date_only = bool(current.get('all_day')) and (not when_iso or valid_due_date(when_iso))
    old_when = float(current["when_ts"])
    if when_iso:
        if valid_due_date(when_iso) and not date_only:
            return '(error: a timed update requires a clock; nothing was changed.)'
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
    if date_only:
        from service.assistant.verified_reminders import due_timestamp
        new_when = due_timestamp({'all_day': True, 'due_date': target.date().isoformat()})
        target = datetime.fromtimestamp(new_when)
    if ((target.date() < datetime.now().date()) if date_only else new_when < time.time() - 60):
        return (f"(error: preserving the old time would put the reminder in the past "
                f"({target:%a %b %-d at %-I:%M %p}); ask what time today to use. "
                "The reminder was not changed.)")

    group = [current] + [row for row in
                         (assistant_store.get(cid)
                          for cid in current.get("duplicate_ids") or []) if row]
    final_title = new_title.strip() or current["title"]
    when_label = target.strftime('%a %b %-d, %Y') + ' (all day; no timed alarm)' if date_only else target.strftime('%a %b %-d at %-I:%M %p')
    date_flags = {'all_day': date_only, **({'due_date': target.date().isoformat()} if date_only else {})} if current.get('all_day') or date_only else {}
    reminder_rows = [row for row in group if row.get("source") == "reminders"]
    if not reminder_rows:
        # No native twin exists (a pre-A18 or Wisp-only reminder), so a local
        # move changes nothing outside Wisp and needs no native receipt.
        manual_ids = [row["id"] for row in group if row.get("source") == "manual"]
        if not manual_ids:
            return "(error: this item is not a Wisp or Apple reminder; nothing was changed.)"
        if assistant_store.update_schedule(manual_ids, new_when, final_title, **date_flags) < 1:
            return "(error: the reminder changed before it could be updated; try again.)"
        from service.assistant.hub import hub
        await hub.publish({"type": "changed"})
        return f"Reminder updated: “{final_title}” — {when_label} in Wisp."
    if len(reminder_rows) > 1:
        return "(error: several native reminders share this item; select an exact reminder before changing it.)"
    if any(not row.get("source_id") for row in reminder_rows):
        return "(error: exact Reminders identity is unavailable; nothing was changed.)"
    from service.assistant.outbox import request as app_request
    from service.assistant.hub import hub
    for row in reminder_rows:
        payload = {
            "source_id": row["source_id"], "expected_title": row["title"],
            "expected_due_ts": row["when_ts"], **expected_date_fields(row), "title": final_title,
            **({'all_day': True, 'due_date': target.date().isoformat()} if date_only else {'due_ts': new_when})}
        result = await app_request("update_reminder", payload)
        if result.get("ok") is not True:
            return f"(error: {result.get('error') or 'Reminder update was not verified'}; check the exact reminder before another attempt.)"
        if date_only:
            try:
                validate_result('update_reminder', payload, result)
            except (KeyError, TypeError, ValueError):
                return '(error: the updated all-day date was not verified; check Reminders before retrying.)'
    manual_ids = [row["id"] for row in group if row["source"] == "manual"]
    if manual_ids:
        assistant_store.update_schedule(manual_ids, new_when, final_title, **date_flags)
    await hub.publish({"type": "changed"})

    return f"Reminder updated: “{final_title}” — {when_label}."


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



def event_move_plan(title: str, when_iso: str, duration_min: int | None = None,
                    location: str = "", new_title: str = "", current_when_iso: str = "",
                    *, now: float | None = None) -> dict:
    """What update_event would do, without doing it: the exact synced occurrence, its
    new start and length, and the native payload. {"error": text} when it can't run.

    Shared by the tool and its confirmation card, so the card names the same event
    and times the move will use."""
    if (location or "").strip() or (new_title or "").strip():
        return {"error": "Wisp can move an event to a new time, but it can't rename an "
                         "event or change its location. Nothing was changed; edit that in Calendar."}
    if problem := calendar_time_problem(when_iso):
        return {"error": problem}
    when, _ = _calendar_local_start(when_iso)
    now = time.time() if now is None else now
    if when.timestamp() < now - 60:
        return {"error": f"({when_iso} is in the past — nothing was moved.)"}
    if duration_min is not None and (type(duration_min) is not int or not 1 <= duration_min <= 10080):
        return {"error": "(error: a new length must be 1–10080 minutes; nothing was moved.)"}
    current = None
    if (current_when_iso or "").strip():
        if calendar_time_problem(current_when_iso):
            return {"error": "(error: the event's current start time is invalid; nothing was moved.)"}
        current = _calendar_local_start(current_when_iso)[0].timestamp()
    found = assistant_store.calendar_move_target(title, now=now, current_start=current)
    if "error" in found:
        return {"error": found["error"]}
    row = found["row"]
    # A subscribed or otherwise read-only calendar: the app would refuse the write, so the
    # confirmation card is never shown. 0/1 from the store, or None when the app didn't say.
    if row.get("calendar_writable") is not None and not row["calendar_writable"]:
        return {"error": f"“{row['title']}” is on a read-only calendar; Wisp can't change it from "
                         "this Mac. Nothing was moved. Move it in Calendar if it is yours to move."}
    # Invitees: only the organizer may move such an event, and Calendar then sends them
    # the new time; the card says so. Unknown either way is refused, never guessed.
    invitees, organizer = row.get("invitee_count"), row.get("organizer_is_user")
    if invitees is None:
        return {"error": f"Wisp can't tell yet whether “{row['title']}” has invitees (Calendar "
                         "hasn't synced that); nothing was moved. Try again shortly or move it in Calendar."}
    # Someone else's event never moves, whatever its invitee count (an invitation can list
    # only the user as attendee). organizer_is_user is 0/1 from the store, or None.
    if organizer is not None and not organizer:
        return {"error": f"Only the organizer can move “{row['title']}”; nothing was moved. "
                         "Ask them, or propose a new time in Calendar."}
    if invitees and organizer is None:
        return {"error": f"Wisp can't tell who organizes “{row['title']}”, so it won't move an "
                         "event with invitees; nothing was moved. Move it in Calendar."}
    if row["end_ts"] is not None and row["end_ts"] >= row["when_ts"]:
        length = duration_min if duration_min is not None else max(1, round((row["end_ts"] - row["when_ts"]) / 60))
    else:
        length = duration_min
    if abs(when.timestamp() - row["when_ts"]) < 60 and duration_min is None:
        return {"error": f"“{row['title']}” already starts then; nothing was moved."}
    return {"row": row, "when_ts": when.timestamp(), "length_min": length,
            "payload": {"source_id": row["source_id"], "expected_title": row["title"],
                        "expected_start_ts": row["when_ts"], "when_ts": when.timestamp(),
                        "duration_min": duration_min, "invitee_count": invitees}}


# The occurrence the user confirmed, set by the agent loop around the approved call
# (see prepare_event_move). update_event then moves exactly that occurrence and never
# re-resolves the title, so the card and the write can't name different events.
_MOVE_BINDING: contextvars.ContextVar[dict | None] = contextvars.ContextVar("event_move_binding", default=None)


@contextlib.contextmanager
def bound_event_move(binding: dict | None):
    token = _MOVE_BINDING.set(binding)
    try:
        yield
    finally:
        _MOVE_BINDING.reset(token)


def prepare_event_move(args: dict) -> dict:
    """Resolve an update_event call once, before its confirmation card.

    Returns {"error": text} (no card: the text is the tool result) or {"args": args with
    the start pinned to this Mac's UTC offset, "binding": the exact occurrence and native
    payload, "preview": the card line}."""
    if problem := calendar_time_problem(args.get("when_iso")):
        return {"error": problem}
    bound = {**args, "when_iso": bind_calendar_local_start(args["when_iso"])}
    plan = event_move_plan(bound.get("title", ""), bound["when_iso"], bound.get("duration_min"),
                           bound.get("location", ""), bound.get("new_title", ""),
                           bound.get("current_when_iso", ""))
    if "error" in plan:
        return {"error": plan["error"]}
    row = plan["row"]
    old = datetime.fromtimestamp(row["when_ts"])
    new = (calendar_interval_label(bound["when_iso"], plan["length_min"]) if plan["length_min"]
           else datetime.fromtimestamp(plan["when_ts"]).strftime("%a %b %-d, %Y %-I:%M %p"))
    once = " (this occurrence only)" if row["recurring"] else ""
    invitees = plan["payload"]["invitee_count"]
    notice = (f" Its {invitees} invitee{'s' if invitees != 1 else ''} will be notified of the new time."
              if invitees else "")
    return {"args": bound,
            "binding": {"payload": plan["payload"], "title": row["title"], "recurring": row["recurring"]},
            "preview": f"Move: {row['title']} — from {old:%a %b %-d %-I:%M %p} to {new}{once}.{notice}"}


@register(
    "update_event",
    "Move an existing event in the user's macOS Calendar to a new time (reschedule "
    "it). Matched by its title; pass `current_when_iso` only to pick one of several "
    "same-titled events. Only the time changes: notes, location, attendees, alerts "
    "and calendar are kept, and for a repeating event only that one occurrence moves. "
    "The length stays the same unless `duration_min` is given. Always confirmed. It "
    "cannot rename an event or change its location, and an event with invitees moves "
    "only when the user organizes it (Calendar then notifies them).",
    {"type": "object",
     "properties": {
         "title": {"type": "string", "description": "the event's current title (any distinctive part)"},
         "when_iso": {"type": "string", "description": "NEW local start datetime, e.g. 2026-07-14T15:00"},
         "duration_min": {"type": "integer",
                          "description": "new length in minutes, only when the user asks for one; omit to keep it"},
         "current_when_iso": {"type": "string",
                              "description": "the event's CURRENT local start, only when several events share the title"},
         "location": {"type": "string", "description": "not supported; leave empty"},
         "new_title": {"type": "string", "description": "not supported; leave empty"},
     },
     "required": ["title", "when_iso"]},
    category="calendar_write",
    aliases=["move my dentist appointment to 3pm", "reschedule the meeting to tomorrow",
             "push my lunch back an hour", "move the standup to 10am"],
    retrieval_description=(
        "Move an existing calendar event to a new time — reschedule it — keeping its "
        "details. Matched by its current title, like cancel_event."),
)
async def update_event(title: str, when_iso: str, duration_min: int | None = None,
                       location: str = "", new_title: str = "", current_when_iso: str = "") -> str:
    # A native, verified move of one exact occurrence (CalendarReader.moveEvent): the app
    # checks the event still has this title and start, changes only its start and end,
    # and reads back that nothing else changed. Never cancel + recreate.
    binding = _MOVE_BINDING.get()
    if binding is not None:
        # The approved call: move exactly the confirmed occurrence, if it is still there
        # as the card showed it, to exactly the confirmed start.
        payload, row = binding["payload"], binding
        if (calendar_time_problem(when_iso)
                or abs(_calendar_local_start(when_iso)[0].timestamp() - payload["when_ts"]) >= 1
                or not assistant_store.calendar_occurrence_current(
                    payload["source_id"], payload["expected_start_ts"], payload["expected_title"])):
            return (f"(couldn't move “{binding['title']}”: it changed after you confirmed. "
                    "Nothing was changed.)")
    else:
        # Only the agent loop's confirmed, bound call may move an event; any other caller
        # (a direct run_tool, a workflow executor) gets the reason and nothing runs.
        plan = event_move_plan(title, when_iso, duration_min, location, new_title, current_when_iso)
        if "error" in plan:
            return plan["error"]
        return ("(error: a Calendar move needs the user's confirmation of the exact event; "
                "nothing was moved.)")
    from service.assistant.outbox import app_connected, request as app_request
    if not await app_connected():
        return EVENT_UPDATE_UNAVAILABLE
    result = await app_request("move_calendar_event", payload)
    if result.get("ok") is not True:
        if result.get("status") == "unknown":
            return (f"(not confirmed: Calendar may have moved “{row['title']}”; check it in "
                    "Calendar before trying again.)")
        return f"(couldn't move “{row['title']}”: {result.get('error') or 'Calendar did not confirm the move'}. Nothing was changed.)"
    from service.assistant.hub import hub
    await hub.publish({"type": "changed"})
    start = datetime.fromtimestamp(result["when_ts"])
    end = datetime.fromtimestamp(result["end_ts"])
    once = " Only this occurrence moved." if row["recurring"] else ""
    notified = " Invitees were notified of the new time." if payload["invitee_count"] else ""
    return f"Moved “{row['title']}” to {start:%a %b %-d, %-I:%M %p}–{end:%-I:%M %p}.{once}{notified}"


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
        # A reminder the stale policy hides is still cancellable by name: hiding an item
        # must never stop the user removing it.
        matches = [c for c in assistant_store.history(days=365, include_stale=True)
                   if q in c["title"].lower()]
    if not matches:
        return f"Nothing upcoming or past matches “{title}”."
    if len(matches) > 1:
        lst = "; ".join(
            f"{c['title']} ({datetime.fromtimestamp(c['when_ts']).strftime('%a %-I:%M %p')})"
            for c in matches[:5])
        return f"Several items match “{title}”: {lst}. Which one? Please be more specific."
    c = matches[0]
    read_only = _read_only_calendar_row(c)
    if read_only:
        return (f"“{read_only['title']}” is on a read-only calendar; Wisp can't change it from this Mac. "
                "Nothing was cancelled. Cancel it in Calendar if it is yours to cancel.")
    from service.assistant.hub import hub
    error = await _retire(c)
    if error:
        return f"(error: {error})"
    await hub.publish({"type": "changed"})
    return f"Cancelled “{c['title']}”."


def _read_only_calendar_row(c: dict) -> dict | None:
    """The calendar row of this item (or its collapsed twins) that this Mac can't
    change: a subscribed/read-only EventKit calendar (writable=0), or a Google
    Calendar event read over the API (source_id gcal:..., never in EventKit)."""
    ids = [c.get("id")] + list(c.get("duplicate_ids") or [])
    for cid in ids:
        row = c if cid == c.get("id") else assistant_store.get(cid)
        if not row or row.get("source") != "calendar":
            continue
        if str(row.get("source_id") or "").startswith("gcal:"):
            return row
        with assistant_store.transaction(write=False) as db:
            flag = db.execute("SELECT writable FROM calendar_event_writable WHERE commitment_id=?",
                              (cid,)).fetchone()
        if flag is not None and not flag["writable"]:
            return row
    return None


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
                "expected_due_ts": row["when_ts"], **expected_date_fields(row)})
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
    # Includes reminders the stale policy hides: "clear my old reminders" is exactly how
    # they should leave, and the confirmation card reads through this same helper.
    items = assistant_store.history(days=max(1, min(int(days or 365), 3650)), include_stale=True)
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
                       *, now: float | None = None, include_stale: bool = False) -> list[dict]:
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

    items = assistant_store.active_between(start, end, include_stale=include_stale)
    items = [c for c in items
             if c.get("source") in _REMINDER_SOURCES
             or bool(_REMINDER_SOURCES & set(c.get("duplicate_sources") or []))]
    needle = (query or "").strip().lower()
    # "all", "*" or "everything" from the model means list them all, not a
    # reminder titled "all" (2026-10-29 Ask run: "you have no reminders").
    if needle in {"all", "*", "any", "everything", "reminder", "reminders", "my reminders",
                  "tasks", "task", "to-dos", "todos", "to do", "todo", "due", "due soon",
                  "upcoming", "overdue", "open", "pending", "incomplete"}:
        needle = ""
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


# A time word passed as the query is a time scope, not a title: "overdue" listed
# every reminder and the model called them all overdue (0.5.0 dev8 audit).
_OVERDUE_WORDS = frozenset({"overdue", "past due", "past-due", "late", "missed"})
_DUE_SOON_WORDS = frozenset({"due soon", "due", "upcoming", "coming up", "soon", "next"})
DUE_SOON_DAYS = 7


def _reminder_query_scope(query: str, scope: str) -> tuple[str, str, int | None]:
    """(query, scope, horizon in days) with a time word in the query applied as a
    scope: "overdue" → past_due, "due soon" → upcoming within DUE_SOON_DAYS."""
    needle = (query or "").strip().lower()
    if (scope or "all").strip().lower() == "all":
        if needle in _OVERDUE_WORDS:
            return "", "past_due", None
        if needle in _DUE_SOON_WORDS:
            return "", "upcoming", DUE_SOON_DAYS
    return query, scope, None


@register(
    "search_reminders",
    "The user's to-dos, tasks and reminders: open, pending, overdue, due soon, "
    "school or work tasks. Leave query empty to list them; set it only to words "
    "from a reminder's title (never 'due', 'tasks' or a date). "
    "Searches current Apple Reminders and retained Wisp-only reminder records "
    "by title, including overdue items. Calendar events are excluded. A "
    "Wisp-only record may be a historical mirror whose Apple copy was deleted; "
    "show it separately for review, never call it a current Apple item. "
    "Storage source labels do not identify who created a reminder.",
    {"type": "object",
     "properties": {
         "query": {"type": "string", "description": "title words to match; empty lists every reminder in scope"},
         "scope": {"type": "string",
                   "enum": ["today", "tomorrow", "past_due", "upcoming", "all"],
                   "description": "time scope; all includes overdue active reminders"},
     },
     "required": []},
    category="assistant_read",
    aliases=["when is my vaccine reminder", "find my dentist reminder",
             "what does my reminder say"],
)
async def search_reminders(query: str = "", scope: str = "all") -> str:
    query, scope, horizon = _reminder_query_scope(query, scope)
    from service.assistant.sync_status import ensure_sources
    readiness = await ensure_sources(("reminders",), timeout_seconds=4.0)
    state = next((source["state"] for source in readiness["sources"]
                  if source["id"] == "reminders"), "unavailable")
    if state == "unavailable":
        rows = [source for source in readiness["sources"] if source["id"] == "reminders"]
        notice = ("Wisp could not check Apple Reminders. " + _access_fixes(rows)
                  + "No Apple Reminders item is listed or verified.")
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
    if horizon is not None:
        cutoff = time.time() + horizon * 86400
        items = [c for c in items if float(c.get("when_ts") or 0) <= cutoff]
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
    filtered = ("Only reminders already overdue (due before now) are listed." if scope == "past_due"
                and not query else f"Only reminders due in the next {horizon} days are listed."
                if horizon is not None else "")
    if not items and not undated_section and filtered:
        return ("A current Reminders read found " + ("no overdue reminders." if scope == "past_due"
                else f"nothing due in the next {horizon} days.") + stale_note)
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
    if filtered:
        sections.append(filtered)
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
    # A removal reaches reminders the stale policy hides; the confirmation previews in
    # agent/loop.py read with the same flag, so the card and the deletion agree.
    items = reminders_matching(normalized, query, include_stale=True)
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


