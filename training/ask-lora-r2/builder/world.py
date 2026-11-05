"""Synthetic world for Sam Rivera (fictional CS sophomore) and tool-output
emulators that reproduce the real tools' output shapes.

get_upcoming / get_past_events reuse Wisp's own pure formatting helpers
(resolve_span, _collapse_schedule_rows, _match_schedule_query,
_format_forward_agenda, _fmt_past) on synthetic rows; web_search uses the
real SearchHit/PublicSearchToolResult evidence builders. Everything else is
emulated from the real formatter code and observed output shapes.
"""
from __future__ import annotations

import contextlib
import copy
import json
import datetime as dt
import os
import math
import random
import re
import time as _time
from dataclasses import dataclass, field
from unittest import mock

import harness as H
from service.tools import assistant_tools as AT
from service.tools.timeranges import BadPeriod, resolve_span

USER_EMAIL = H.USER_EMAILS[0]


def _size_bytes(size) -> float:
    """'2.4 MB' / '310 KB' / 1234 -> bytes (list_dir sort='size')."""
    if isinstance(size, (int, float)):
        return float(size)
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?)B?", str(size), re.I)
    if not m:
        return 0.0
    return float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[m.group(2).upper()]


def short_clock(t: dt.datetime) -> str:
    """10 AM / 1:30 PM."""
    return t.strftime("%-I %p") if t.minute == 0 else t.strftime("%-I:%M %p")


# --- data -----------------------------------------------------------------------
@dataclass
class Event:
    title: str
    start: dt.datetime
    minutes: int = 60
    location: str = ""
    kind: str = "event"         # event | meeting | exam | assignment
    all_day: bool = False
    context: str = ""
    # Round-2 move/cancel facts (never printed by the read tools):
    invitees: int | None = 0            # None = Calendar hasn't synced the attendee count
    organizer: bool | None = True       # None = unknown organizer
    writable: bool = True               # False = read-only (subscribed/shared) calendar
    series: str = ""                    # same non-empty value = occurrences of one repeating event

    @property
    def end(self):
        return self.start + dt.timedelta(minutes=self.minutes)


@dataclass
class Reminder:
    title: str
    due: dt.datetime | None
    done: bool = False


@dataclass
class Email:
    sender: str
    addr: str
    subject: str
    when: dt.datetime
    body: str = ""
    unread: bool = True
    person: bool = True          # from a person (vs newsletter/update)
    to: str = USER_EMAIL
    account: str = USER_EMAIL
    message_id: str = ""         # round 2: printed by view_emails only when set (reply_to_email)


@dataclass
class Msg:
    conv: str                    # conversation label (the other person, or "Group of N (...)")
    sender: str                  # "Me" or the person's name
    text: str
    when: dt.datetime
    unread: bool = False


@dataclass
class Note:
    title: str
    body: str
    modified: dt.datetime
    folder: str = "Notes"


@dataclass
class BriefItem:
    """A dated to-do Wisp's Brief read from mail, texts or notes (proactive_tools.BRIEF_SOURCES): a family lunch from
    a text, a homework due time from a Canvas email. `ref` is the Email / Msg / Note it was read from, so a
    re-read of that source (view_messages, view_emails, search_notes) finds the same words."""
    title: str                   # the Brief's to-do title ("Finish Problem Set 5, Linear Algebra")
    due: dt.datetime             # date-only items: any time on that day (the Brief stores 11:59 PM)
    source: str                  # mail | messages | notes
    who: str = ""
    timed: bool = False          # the source states a clock time ("due Fri 11:59 PM")
    ref: object = None


@dataclass
class Waiting:
    """A person the Brief lists as waiting on a reply (cycle.with_waiting_people)."""
    who: str
    summary: str                 # what they asked, as the Brief states it
    source: str = "messages"     # messages | mail
    title: str = ""
    when: dt.datetime | None = None
    asked: bool = True


class _BriefStore:
    """The slice of AssistantStore the Brief reads (sources.schedule, proactive_tools._store_schedule), served from a
    World's events and reminders. A Calendar event is a commitment row with its end; Apple Reminders a week or more
    overdue are hidden, as the store hides them (assistant.stale_reminders)."""

    def __init__(self, world):
        self.w = world
        self.rows = []
        for i, ev in enumerate(world.events):
            self.rows.append({"id": f"evt-{i}", "title": ev.title, "when_ts": world._zts(ev.start),
                              "end_ts": world._zts(ev.end), "source": "calendar", "kind": ev.kind,
                              "location": ev.location, "all_day": ev.all_day, "source_id": f"evt-{i}",
                              "context": ev.context, "account": "iCloud"})
        for i, r in enumerate(world.reminders):
            if r.due is not None and not r.done:
                self.rows.append({"id": f"rem-{i}", "title": r.title, "when_ts": world._zts(r.due), "source": "reminders",
                                  "kind": "reminder", "all_day": False, "source_id": f"rem-{i}"})
        self.rows.sort(key=lambda r: r["when_ts"])

    def upcoming(self, now=None, days=7):
        return [dict(r) for r in self.rows if now - 300 <= r["when_ts"] <= now + days * 86400]

    def hidden_commitment_ids(self, now=None):
        return {r["id"] for r in self.rows if r["source"] == "reminders" and now - r["when_ts"] >= 7 * 86400}

    def undated_reminders(self, query=""):
        return [{"source_id": f"undated-{i}", "title": r.title, "context": ""}
                for i, r in enumerate(self.w.reminders) if r.due is None and not r.done]

    def calendar_events_overlapping(self, start_ts, end_ts):
        return [dict(r) for r in self.rows if r["source"] == "calendar" and r["when_ts"] < end_ts
                and (r["when_ts"] >= start_ts or r["end_ts"] > start_ts)]

    def active_between(self, start_ts=None, end_ts=None, *, include_stale=False):
        return [dict(r) for r in self.rows if (start_ts is None or r["when_ts"] >= start_ts)
                and (end_ts is None or r["when_ts"] < end_ts)]


@dataclass
class World:
    now: dt.datetime
    events: list = field(default_factory=list)
    reminders: list = field(default_factory=list)
    emails: list = field(default_factory=list)
    sent_mail: list = field(default_factory=list)     # (to, subject, when) for view_emails(mailbox="sent")
    messages: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    past: list = field(default_factory=list)          # past calendar events
    files: dict = field(default_factory=dict)         # folder -> [(name, size)]
    patterns: list = field(default_factory=list)      # describe_me lines
    memories: list = field(default_factory=list)      # recall facts (text)
    weather: dict = field(default_factory=dict)       # location -> dict
    web: dict = field(default_factory=dict)           # query -> list[(title, url, snippet)]
    contacts: dict = field(default_factory=dict)      # name -> (phone, email)
    overrides: dict = field(default_factory=dict)     # tool -> callable(args) | str
    unavailable: set = field(default_factory=set)     # sources: calendar, reminders, mail, messages, notes
    status: dict = field(default_factory=dict)
    effects: list = field(default_factory=list)       # round 2: (tool, args) of every write that took effect
    brief: list = field(default_factory=list)         # BriefItem: dated to-dos read from mail, texts and notes
    waiting: list | None = None                       # Waiting people; None = the first two unread incoming texts

    # ---------------------------------------------------------------- dispatch
    # service/tools/memory_tools.py: the request must open with remember/save/update/correct, and `fact`
    # must be a case-sensitive substring of the request (main.py sets the quote to the raw prompt).
    _REMEMBER_SAVE = re.compile(r'^(?:(?:please|can you|could you|i want you to)\s+)*(?:remember|save|update|correct)\b', re.I)
    _REMEMBER_CORRECT = re.compile(r'\b(?:update|correct|correction|instead|no longer|now|actually)\b', re.I)
    _REMEMBER_RISK = re.compile(r'\b(?:hypothetical|example|pretend|fiction|story|roleplay|test|fixture|quote|quoted|said|wrote)\b|["“”`>]', re.I)

    def remember_refusal(self, args: dict, prompt: str) -> str | None:
        """The real `remember` tool's refusal text for this call, or None when it would save."""
        quote = prompt or ""
        fact = args.get("fact")
        if not self._REMEMBER_SAVE.search(quote.strip()):
            return ("Nothing saved. Explicit saving requires the current user to ask to remember, save, "
                    "update or correct a statement.")
        if not isinstance(fact, str) or not fact.strip() or fact not in quote:
            return ("Nothing saved. Copy the complete statement exactly from the current user request; "
                    "do not infer or paraphrase.")
        after = quote.split(fact, 1)[1]
        if self._REMEMBER_RISK.search(quote) or (after.strip() and not re.match(r"\s*[.!?\n]", after)
                and not (re.search(r"[.!?]\s*$", fact) and re.match(r"\s+\S", after))):
            return ("Nothing saved. The statement is quoted, ambiguous, or missing a qualification. "
                    "Use Memory review to save the intended wording explicitly.")
        if args.get("supersedes_id") is not None and not self._REMEMBER_CORRECT.search(quote):
            return "Nothing changed. A correction needs an explicit current request and an active memory ID."
        return None

    def handle(self, name: str, args: dict):
        if name == "remember" and H.CURRENT.get("prompt") is not None:
            # A scripted `remember` the real tool would refuse is recorded; harness.run_scenario then fails
            # the scenario (unless the call is a Bad step), so no row teaches "saved" for a refused call.
            if (msg := self.remember_refusal(args, H.CURRENT["prompt"])) is not None:
                H.CURRENT["violations"].append(("remember", copy.deepcopy(args), msg))
                return msg
        if name in self.overrides:
            o = self.overrides[name]
            out = o(args) if callable(o) else o
            if isinstance(out, Exception):
                raise out
            return out
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            raise RuntimeError(f"synthetic world has no handler for {name}")
        try:
            return fn(**args)
        except TypeError as exc:
            # A wrong argument name must read exactly as the real tool's TypeError
            # does ("summarize_emails() got an unexpected keyword argument ..."),
            # never with this class's method name.
            text = str(exc).replace(f"World.t_{name}()", f"{name}()")
            if text != str(exc):
                raise TypeError(text) from None
            raise

    # ---------------------------------------------------------------- helpers
    @property
    def ts(self):
        return self.now.timestamp()

    def _span(self, period, forward):
        start, end, label = resolve_span(period, forward=forward)
        return dt.datetime.fromtimestamp(start), dt.datetime.fromtimestamp(end), label

    def _row(self, ev: Event, i: int) -> dict:
        return {"title": ev.title, "when_ts": ev.start.timestamp(), "end_ts": ev.end.timestamp(),
                "source": "calendar", "kind": ev.kind, "location": ev.location,
                "all_day": ev.all_day, "source_id": f"evt-{i}", "context": ev.context,
                "account": "iCloud"}

    def _rrow(self, r: Reminder, i: int) -> dict:
        return {"title": r.title, "when_ts": r.due.timestamp(), "source": "reminders",
                "kind": "reminder", "source_id": f"rem-{i}"}

    # ------------------------------------------------------------- calendar
    def upcoming_rows(self, days=7, period=None, calendar_only=False, query=None, account=None, include_holidays=False):
        """(rows, window_label, span_start) exactly as t_get_upcoming selects them."""
        now = self.ts
        window_label = f"next {days} day(s)"
        start_day = dt.datetime.combine(self.now.date(), dt.time())
        s = None
        if period:
            s, e, window_label = resolve_span(period, forward=True)   # BadPeriod propagates
            today_period = (s == start_day.timestamp()
                            and e == (start_day + dt.timedelta(days=1)).timestamp())
            fwd = max(s, now)
            cal = [self._row(ev, i) for i, ev in enumerate(self.events)
                   if ev.start.timestamp() < e and ev.end.timestamp() > (s if today_period else fwd)]
            rem = [self._rrow(r, i) for i, r in enumerate(self.reminders)
                   if r.due and not r.done and fwd <= r.due.timestamp() < e]
        else:
            end = now + int(days) * 86400
            cal = [self._row(ev, i) for i, ev in enumerate(self.events)
                   if ev.start.timestamp() <= end and ev.end.timestamp() > now]
            rem = [self._rrow(r, i) for i, r in enumerate(self.reminders)
                   if r.due and not r.done and now <= r.due.timestamp() <= end]
        if "reminders" in self.unavailable:
            rem = []
        items = cal + rem
        if calendar_only:
            items = [r for r in items if r["source"] == "calendar"]
        items = AT._filter_account(items, account)
        if query:
            items = AT._match_schedule_query(items, query)
        items = AT._without_holiday_calendars(items, include_holidays=bool(include_holidays))
        items = AT._collapse_schedule_rows(
            [r for r in items if r["when_ts"] >= now or (period and r["source"] == "calendar")])
        return items, window_label, s

    def t_get_upcoming(self, days=7, period=None, calendar_only=False, boundary="start",
                       query=None, account=None, include_holidays=False):
        if boundary not in {"start", "end"} or (boundary == "end" and (not calendar_only or not query)):
            return "(error: scheduled end projection requires a named Calendar-only query)"
        if "calendar" in self.unavailable:
            return ("Wisp could not check Calendar. Turn on Calendar access in System Settings › "
                    "Privacy & Security › Calendars, then try again. This schedule may be incomplete.\n"
                    "No scheduled items were found in the sources that could be checked.")
        days = max(1, min(int(days or 7), 60))
        today_str = self.now.strftime("%A, %B %-d, %Y")
        try:
            items, window_label, s = self.upcoming_rows(days, period, calendar_only, query, account, include_holidays)
        except BadPeriod as exc:
            return f"(error: {exc})"
        start_day = dt.datetime.combine(self.now.date(), dt.time())
        note = ("\nWisp has not received a current Reminders read yet, so Apple Reminders items "
                "are not shown here. Try again in a moment." if "reminders" in self.unavailable else "")
        # assistant_tools.get_upcoming: dated to-dos the Brief read from mail, texts and notes follow the agenda,
        # apart from it and never counted as calendar events (a named end-time question is Calendar only).
        brief = ""
        if boundary != "end":
            brief = self._upcoming_brief(items, days, period, query, account)
        if not items:
            if brief:
                what = "on your calendar" if calendar_only else "on your calendar or reminders"
                return f"Today is {today_str}. Nothing {what} in {window_label}.\n\n" + brief + note
            if note:
                return "No scheduled items were found in the sources that could be checked." + note
            return f"Today is {today_str}. Nothing scheduled in {window_label}."
        if boundary == "end":
            return AT._format_calendar_end_times(items, now=self.ts, window_label=window_label)
        return AT._format_forward_agenda(
            items, now=self.ts, window_label=window_label,
            apple_reminders_checked="reminders" not in self.unavailable,
            reminders_read=not calendar_only and "reminders" not in self.unavailable,
            display_day_start=max(s, start_day.timestamp()) if period else None) + (
                "\n\n" + brief if brief else "") + note

    def _upcoming_brief(self, items, days, period, query, account) -> str:
        """get_upcoming's Brief section and pointer, from the live helpers over this world's Brief snapshot."""
        from service.tools import proactive_tools as PT
        now = self.ts
        if period:
            start, end, _label = resolve_span(period, forward=True)
            brief_start = max(start, now)
        else:
            end, brief_start = now + days * 86400, now
        with self.brief_live():
            snap = PT.read_brief_snapshot(now)
            found = PT.dated_brief_items(snap, brief_start, end, account=account)
            if query:
                found = AT._match_schedule_query(found, query)
            shown = {AT._agenda_text(item.get("title")).casefold() for item in items}
            brief = PT.brief_section([t for t in found if AT._agenda_text(t.get("title")).casefold() not in shown])
            if not query:
                more = PT.brief_pointer(snap, now=now, day=period or "this_week")
                brief = "\n".join(part for part in (brief, more) if part)
        return brief

    def t_get_past_events(self, days=30, query=None, account=None, include_holidays=False):
        days = max(1, min(int(days or 30), 365))
        now = self.ts
        today_str = self.now.strftime("%A, %B %-d, %Y")
        rows = []
        for i, ev in enumerate(self.past):
            if self.now - dt.timedelta(days=days) <= ev.start < self.now:
                r = self._row(ev, 1000 + i)
                rows.append(r)
        for i, r in enumerate(self.reminders):
            if r.due and self.now - dt.timedelta(days=days) <= r.due < self.now:
                rows.append({"title": r.title, "when_ts": r.due.timestamp(), "kind": "reminder",
                             "source": "reminders", "context": "Reminders"})
        if query:
            q = query.lower()
            rows = [c for c in rows if q in c["title"].lower() or q in (c.get("context") or "").lower()]
        rows.sort(key=lambda r: -r["when_ts"])
        if not rows:
            return (f"Today is {today_str}. Nothing found in the past {days} day(s)"
                    + (f" matching {query!r}" if query else "") + ". "
                    "(Calendar history only reaches back to whenever Wisp started "
                    "syncing it, up to about a year.)")
        return (f"Today is {today_str}. Past {days} day(s), {len(rows)} item(s), most recent first:\n"
                + "\n".join(AT._fmt_past(c, now) for c in rows))

    def t_search_reminders(self, query="", scope="all"):
        """assistant_tools.search_reminders: the reminder list, then the Brief's open mail / text / note to-dos in
        the same scope, apart from it and labelled as not reminders."""
        text = self._search_reminders(query, scope)
        with self.brief_live():
            brief = AT._reminders_brief(query, scope)
        return text + ("\n\n" + brief if brief else "")

    def _search_reminders(self, query="", scope="all"):
        if "reminders" in self.unavailable:
            return ("Wisp could not check Apple Reminders. Turn on Reminders access in System Settings › "
                    "Privacy & Security › Reminders. No Apple Reminders item is listed or verified."
                    f"\nNo Wisp-only reminder record matches {query!r}.")
        today = self.now.date()
        out, undated = [], []
        for r in self.reminders:
            if r.done:
                continue
            if query and not all(w in r.title.lower() for w in query.lower().split()):
                continue
            if r.due is None:
                undated.append(r)
                continue
            d = r.due.date()
            ok = {"today": d == today, "tomorrow": d == today + dt.timedelta(days=1),
                  "past_due": r.due < self.now, "upcoming": r.due >= self.now,
                  "all": True}.get(scope, True)
            if ok:
                out.append(r)
        out.sort(key=lambda r: r.due)
        show_undated = scope == "all" and undated
        if not out and not show_undated:
            return (f"A current Reminders read found no active match for {query!r}. "
                    "An older remembered item does not establish a current reminder.")
        sections = ["A fresh Apple Reminders read supersedes older remembered claims. "
                    "These lists describe storage, not who created an item. "
                    "Recently Deleted status is not independently verified."]
        lines = [f"- {r.title} — {r.due:%a %b %-d, %Y at %-I:%M %p}" for r in out]
        sections.append("Apple Reminders incomplete-item matches:\n" +
                        ("\n".join(lines) if lines else "None with a due date."))
        if show_undated:
            sections.append("Undated incomplete reminders (no due date; neither overdue nor upcoming):\n"
                            + "\n".join(f"- {r.title}" for r in undated))
        return "\n".join(sections)

    FREE_TIME_ZONE = "America/Los_Angeles"

    def _availability_snapshot(self, zone):
        """What AssistantStore.today_snapshot returns for one day: a fresh, complete Calendar receipt
        and every event as a commitment row (the live tool decides what blocks time)."""
        now_ts = self.now.replace(tzinfo=zone).timestamp()
        rows = []
        for i, ev in enumerate(self.events):
            rows.append({"id": f"evt-{i}", "title": ev.title, "source": "calendar", "status": "active",
                         "kind": ev.kind, "context": ev.context, "source_id": f"evt-{i}",
                         "when_ts": ev.start.replace(tzinfo=zone).timestamp(),
                         "end_ts": ev.end.replace(tzinfo=zone).timestamp(),
                         "all_day": ev.all_day, "calendar_writable": ev.writable})
        info = {"available": True, "syncing": False, "last_sync": now_ts,
                "diagnostics": {"coverage_start": 0.0, "coverage_end": 4e9}}
        return {"sources": {"calendar": dict(info), "reminders": dict(info)}, "commitments": rows, "tasks": []}

    def t_find_free_time(self, days=3, period="", minutes=30, day_start=9, day_end=18):
        """Runs the live schedule_extras availability code over this world's events, so the text is
        production's: candidate slots `minutes` long with ISO times and 'free until', the scope
        trailer, and the 'No matches:' line."""
        from zoneinfo import ZoneInfo
        from service.tools import schedule_extras as SE
        tz = self.FREE_TIME_ZONE
        zone = ZoneInfo(tz)
        try:
            options = SE._availability_options(days, minutes, day_start, day_end, legacy=not period)
            now = self.now.replace(microsecond=0, tzinfo=zone)
            SE._availability_days(now, options[0], period or "")
            if "calendar" in self.unavailable or "reminders" in self.unavailable:
                raise ValueError("Wisp could not check current Calendar and Reminders availability. No study "
                                 "slot has been verified; try again after the sources finish syncing.")
            snapshot = self._availability_snapshot(zone)
            with mock.patch.object(SE._store, "today_snapshot", lambda key, tzname: copy.deepcopy(snapshot)), \
                    self.brief_live():
                return SE._availability_result(options, period or "", tz, now)
        except (ValueError, TypeError, OverflowError, OSError, KeyError, AttributeError) as exc:
            return f"(error: {exc})"

    def free_ranges(self, period="", **kw):
        """(result text, [(start, free_until)] clock strings of the first day line) read from the
        live 'H:MM AM–H:MM AM (iso → iso; free until iso)' spans: the answer's range ends at the
        'free until' time, not at the end of the candidate slot."""
        res = self.t_find_free_time(period=period, **kw)
        spans = []
        for ln in res.splitlines():
            if "free until" in ln:
                for m in re.finditer(r"(\d+:\d\d [AP]M)–\d+:\d\d [AP]M \([^;]*; free until ([^)]*)\)", ln):
                    until = dt.datetime.fromisoformat(m.group(2))
                    spans.append((m.group(1), f"{until:%-I:%M %p}"))
                break
        return res, spans

    # ------------------------------------------------------------- Wisp's Brief
    # The Brief (service.proactive) holds what the calendar and Reminders say, what mail, texts and notes add
    # (dated to-dos with a due time) and the people waiting on a reply. The tools that read it are the LIVE ones:
    # get_my_day_plan runs unchanged, and get_upcoming, find_free_time, search_reminders and predict_my_day call
    # the live Brief helpers. What stands in for the user's data is (1) a proactive store whose state holds this
    # world's Brief to-dos and waiting people and (2) an assistant store that serves this world's events and
    # reminders. Everything between them, the cycle.snapshot merge, due labels, overview, section order, caps,
    # range and error handling, is production code.
    def _zts(self, when: dt.datetime) -> float:
        from zoneinfo import ZoneInfo
        return when.replace(tzinfo=ZoneInfo(self.FREE_TIME_ZONE)).timestamp()

    def waiting_people(self) -> list[Waiting]:
        """Who the Brief lists as waiting on a reply: `waiting` when the scenario set it, else the first two
        unread incoming texts (the default worlds carry no Brief analysis)."""
        if self.waiting is not None:
            return list(self.waiting)
        return [Waiting(m.conv, m.text, "messages", m.text, m.when)
                for m in [m for m in self.messages if m.sender != "Me" and getattr(m, "unread", False)][:2]]

    def _brief_state(self) -> dict:
        """The proactive store's state for this world: Brief to-dos from mail, texts and notes, waiting people."""
        from service.proactive import guard
        tz, now = self.FREE_TIME_ZONE, self._zts(self.now)
        todos = []
        for i, b in enumerate(self.brief):
            due = self._zts(b.due)
            todos.append({"id": f"brief-{i}", "title": b.title, "source": b.source, "account": USER_EMAIL, "who": b.who,
                          "quote": b.title, "due_ts": due, "kind": "task", "done": False, "card_id": None,
                          "due_label": guard.due_label(due, now, tz, has_clock=b.timed),
                          "fingerprints": [f"brief:{i}"], "asks": []})
        threads = []
        for i, p in enumerate(self.waiting_people()):
            received = self._zts(p.when or self.now - dt.timedelta(hours=3 + i))
            threads.append({"id": f"thread:w{i}", "source": p.source, "account": USER_EMAIL, "who": p.who,
                            "title": p.title or p.summary, "summary": p.summary, "user_wrote": False,
                            "needs_reply": True, "asked": p.asked, "replied": False, "deadline": None,
                            "received_ts": received, "reply_ref": "", "asks": []})
        return {"todos": todos, "threads": threads, "cards": [], "notes": [], "as_of": now, "timezone": tz}

    @contextlib.contextmanager
    def brief_live(self):
        """Run live Brief code over this world: the clock is the world's, the proactive store holds its Brief
        items, and the assistant store serves its events and reminders."""
        import service.assistant.store as assistant_store_module
        import service.tools.proactive_tools as PT
        from service.proactive.store import store as proactive_store
        proactive_store().save_state(**self._brief_state())
        fake_store = _BriefStore(self)
        clock = dict(H._CLOCK)           # the harness clock, frozen at this world's now while live code resolves days
        H.set_clock(self.now)
        try:
            # The harness replaces datetime.date with a subclass; proactive_tools tests isinstance(day, date).
            # assistant_tools binds assistant_store at import, so get_upcoming reads it from there.
            with mock.patch.object(assistant_store_module, "assistant_store", fake_store), \
                    mock.patch.object(AT, "assistant_store", fake_store), \
                    mock.patch.object(_time, "time", lambda: self._zts(self.now)), \
                    mock.patch.object(PT, "date", H._RealDate):
                yield
        finally:
            H._CLOCK.update(clock)

    def t_get_my_day_plan(self, day="today"):
        """service.tools.proactive_tools.get_my_day_plan itself: a day (today, tomorrow, a weekday or date) or a
        range (this_week, this weekend, next week), the live header, sections and clock format, and its errors
        (a past day points at get_past_events, a long range at get_upcoming)."""
        import service.tools.proactive_tools as PT
        with self.brief_live():
            coro = PT.get_my_day_plan(day)
            try:
                coro.send(None)
            except StopIteration as done:
                return done.value
            raise RuntimeError("get_my_day_plan awaited something")

    def t_predict_my_day(self, day="tomorrow"):
        """behavior_tools.predict_my_day with no learned routine: the live label line, then the day's dated Brief
        items from mail, texts and notes."""
        from service.assistant import behavior_runtime
        from service.tools.proactive_tools import brief_block
        if day not in {"today", "tomorrow"}:
            day = "tomorrow"
        start, end = behavior_runtime.day_bounds(day, self.ts)
        label = ("Today" if day == "today" else "Tomorrow") + dt.datetime.fromtimestamp(start).strftime(" (%A, %B %-d)")
        with self.brief_live():
            brief = brief_block(self.ts, start=start, end=end, note="not learned routines")
        return (f"{label}: I don't have enough learned routine to predict anything specific yet."
                + ("\n\n" + brief if brief else ""))

    def t_daily_brief(self, location="", days=1, include_activity=True, *, period="", account="", query="",
                      reminder_days=7, include_overdue=None, include_undated=None, include_calendar=True,
                      include_reminders=True, include_email=True, include_messages=True, include_weather=True):
        parts = [f"Good {'morning' if self.now.hour < 12 else 'afternoon' if self.now.hour < 17 else 'evening'} — "
                 f"{self.now:%A, %B %-d}.", "", "Schedule:", self.t_get_upcoming(period="today"), "",
                 "Reminders:", self.t_search_reminders(scope="all"), "", "Email:", self.t_summarize_emails(day="today")]
        return "\n".join(parts)

    # ------------------------------------------------------------- mail
    def _mail_window(self, period=None, day=None):
        p = period or day
        if not p:
            return None, None, "your recent inbox"
        s, e, label = self._span(p, False)
        return s, e, label

    def t_summarize_emails(self, period=None, day=None, count=20, unread=False, account=None, query=None):
        # Production hands a keyword query to view_emails with the same scope
        # (summarize_emails: "any emails from my bank?" reached it with a query).
        if query and str(query).strip():
            return self.t_view_emails(query=str(query).strip(), day=day,
                                      count=min(max(int(count or 5), 1), 20),
                                      account=account, period=period, unread=unread)
        if "mail" in self.unavailable:
            return ("Mail isn't connected yet, so Wisp can't read your email. Open Mail and add an "
                    "account, or connect Google in Quick Settings.")
        try:
            s, e, label = self._mail_window(period, day)
        except BadPeriod as exc:
            return str(exc)      # production returns the BadPeriod text as is, no "(error:" prefix
        rows = [m for m in self.emails if (s is None or s <= m.when < e) and m.when <= self.now]
        if unread:
            rows = [m for m in rows if m.unread]
        rows.sort(key=lambda m: m.when, reverse=True)
        if count:
            rows = rows[:int(count)]
        read_at = (self.now - dt.timedelta(minutes=7)).strftime("%b %-d, %-I:%M %p")
        head = ("Summarized per account; keep the account headings in your answer.\n\n"
                f"Linden: Email is from Mail’s local cache. Newer messages may be missing; server "
                f"freshness is unverified (last local read {read_at}). To check for newer mail, open "
                "Mail and let it refresh.\n\n")
        if not rows:
            if s is None:
                return head + "No emails found in your recent inbox."
            return head + (f"No emails in {label} ({s:%b %-d, %-I:%M %p} to {e:%b %-d, %-I:%M %p}). "
                           "Scanned 0 matching headers; represented 0 messages; truncated 0 messages.")
        title = "your recent inbox" if s is None else (self.now.strftime("%A, %B %-d") if label == "today" else label)
        people = [m for m in rows if m.person]
        news = [m for m in rows if not m.person]
        unread_n = sum(m.unread for m in rows)
        out = [f"📬 **Inbox digest — {title}**",
               f"{len(rows)} email{'s' if len(rows) != 1 else ''} · {unread_n} unread", ""]
        if people:
            out.append(f"**👤 From people** ({len(people)})")
            for m in people:
                dom = m.addr.split("@")[-1]
                out.append(f"- **{m.sender}** · {dom} — “{m.subject}”" + (" (unread)" if m.unread else ""))
            out.append("")
        if news:
            from collections import Counter
            c = Counter(m.sender for m in news)
            out.append(f"**📰 Newsletters & updates** ({len(news)})")
            out.append(", ".join(f"{k} ({v})" if v > 1 else k for k, v in c.items()))
            out.append("")
        lo, hi = min(m.when for m in rows), max(m.when for m in rows)
        out.append(f"**Coverage:** Scanned {len(rows) + 9} cached headers; represented {len(rows)} messages "
                   f"from {len(set(m.sender for m in rows))} sender notes; truncated 0 known messages. "
                   f"Actual dates: {hi:%b %-d, %Y %-I:%M %p} to {lo:%b %-d, %Y %-I:%M %p}.")
        return head + "\n".join(out)

    def _sent_view(self, query, day, count, account, period):
        """view_emails(mailbox='sent'): headers only, newest first (email_tools.sent_emails_view)."""
        label = "the last 7 days"
        s, e = self.now - dt.timedelta(days=7), self.now + dt.timedelta(seconds=1)
        if period or day:
            try:
                s, e, label = self._mail_window(period, day)
            except BadPeriod as exc:
                return str(exc)
        rows = [r for r in self.sent_mail if s <= r[2] < e]
        if query:
            q = str(query).lower()
            rows = [r for r in rows if q in (r[0] + " " + r[1]).lower()]
        rows.sort(key=lambda r: r[2], reverse=True)
        if not rows:
            what = f" matching “{query}”" if query else ""
            return f"No sent mail{what} in {label} from Apple Mail."
        shown = rows[:max(1, min(int(count or 5), 15))]
        lines = [f"Sent mail, {label} ({len(rows)} found, newest first; headers only):"]
        for to, subject, when in shown:
            lines.append(f"- {when:%a %b %-d, %-I:%M %p} to {to}: {subject}")
        if len(rows) > len(shown):
            lines.append(f"(+{len(rows) - len(shown)} more; narrow it by person or day.)")
        return "\n".join(lines)

    def _view_emails_ranked(self, query, mails, count, unread):
        """Round 5 only: a search through the pinned host's own ranking and budget (PR #476), so rows see what production
        sends: best match first, list mail after, the 6,000-char budget shared, and header lines for what didn't fit.
        None on an older pin without those helpers, which keeps round 4's builds unchanged."""
        if os.environ.get("ASK_LORA_R5") != "1":
            return None
        try:
            from service.tools import email_digest as ED, email_tools as ET
            search, block, omission, line = ET._search_rows, ET._email_body_block, ET._omission_line, ET._mail_line
            fit, budget, shown = ED.fit_rows, ED.RESULT_BUDGET_CHARS, ED.MAX_HEADER_LINES
        except (ImportError, AttributeError):
            return None
        if unread:
            mails = [m for m in mails if m.unread]
        drows = [{"sender": f"{m.sender} <{m.addr}>", "sender_address": m.addr, "to": m.to, "subject": m.subject, "body": m.body,
                  "ts": m.when.timestamp(), "account": m.account, "message_id": m.message_id, "unread": m.unread,
                  "_digested": bool(getattr(m, "digested", False))} for m in mails]
        found, by_words = search(query, drows)
        back_to = min((r["ts"] for r in drows), default=None)
        scope = f"from {ET._short_date(back_to)} on" if back_to is not None else ""
        read_at = (self.now - dt.timedelta(minutes=7)).strftime("%b %-d, %Y, %-I:%M %p")
        note = (f"Linden: Email is from Mail’s local cache. Newer messages may be missing; server freshness is unverified "
                f"(last local read {read_at}). To check for newer mail, open Mail and let it refresh.\n\n")
        if not found:
            return (f"No emails matching “{query}” were found in the recent inbox Wisp searched: the text of {len(drows)} saved "
                    f"{'email' if len(drows) == 1 else 'emails'} {scope}. Read and unread mail were both searched; anything older was not.")
        limit, available = int(count or 5), len(found)
        rows, left = found[:limit], found[limit:]
        reserved = len(note) + 2 + ET._COVERAGE_CHARS
        fitted = fit(rows, query=query, budget=budget - reserved, best_first=True,
                     header_chars=lambda r: len(block({**r, "body": ""})))
        lines = fitted.header_only[:shown]
        coverage = omission(query, len(rows), available, limit, len(left), "lower-ranked", ranked=True, by_words=by_words,
                            unread=unread, searched=len(drows), scope=scope, header_only=len(fitted.header_only),
                            listed=len(lines), cut=fitted.cut)
        trailer = ""
        if lines:
            trailer = ("\n\nNo room for the text of these (look one up on its own to read it):\n"
                       + "\n".join(line(r)[:ED.HEADER_LINE_CHARS] for r in lines))
        return note + coverage + "\n\n---\n\n".join(block(r) for r in fitted.rows) + trailer

    def t_view_emails(self, query=None, period=None, day=None, unread=False, count=5, account=None,
                      strict_match=False, mailbox=None):
        if str(mailbox or "inbox").strip().lower() == "sent":
            return self._sent_view(query, day, count, account, period)
        if "mail" in self.unavailable:
            return ("Mail isn't connected yet, so Wisp can't read your email. Open Mail and add an "
                    "account, or connect Google in Quick Settings.")
        try:
            s, e, label = self._mail_window(period, day)
        except BadPeriod as exc:
            return str(exc)
        rows = [m for m in self.emails if (s is None or s <= m.when < e) and m.when <= self.now]
        ranked = self._view_emails_ranked(query, rows, count, unread) if query else None
        if ranked is not None:
            return ranked
        if query:
            q = query.lower()
            rows = [m for m in rows if q in (m.sender + " " + m.addr + " " + m.to + " " + m.subject + " "
                                             + m.body).lower()]
        if unread:
            rows = [m for m in rows if m.unread]
        rows.sort(key=lambda m: m.when, reverse=True)
        total = len(rows)
        rows = rows[:int(count or 5)]
        read_at = (self.now - dt.timedelta(minutes=7)).strftime("%b %-d, %-I:%M %p")
        head = (f"Linden: Email is from Mail’s local cache. Newer messages may be missing; server freshness "
                f"is unverified (last local read {read_at}). To check for newer mail, open Mail and let it "
                "refresh.\n\n")
        if not rows:
            return head + (f"No emails matching {query!r} were found" if query else "No emails found") + (
                f" for {label}." if s is not None else " in your recent inbox.")
        cov = (f"Result coverage: returned {len(rows)} of {total} available matching emails"
               + (f"; {total - len(rows)} older emails were omitted by count={count or 5}." if total > len(rows) else ".")
               + "\n\n")
        blocks = []
        for m in rows:
            blocks.append(f"Account: {m.account}\nFrom: {m.sender} <{m.addr}>\nTo: {m.to}\n"
                          f"Subject: {m.subject}\nDate: {m.when:%a %b %-d, %Y %-I:%M %p}\n"
                          + (f"Message-ID: {m.message_id}\n" if m.message_id else "")
                          + ("Status: unread\n" if m.unread else "") + f"\n{m.body}")
        return head + cov + "\n\n---\n\n".join(blocks)

    # ------------------------------------------------------------- messages
    def t_summarize_messages(self, period=None, day=None, conversation=None, count=30, unread=False):
        if "messages" in self.unavailable:
            return ("Wisp can't read Messages yet: Full Disk Access is off for Wisp. Turn it on in System "
                    "Settings › Privacy & Security › Full Disk Access, then try again.")
        if period or day:
            try:
                s, e, label = self._span(period or day, False)
            except BadPeriod as exc:
                return str(exc)
            head_label = f"messages from {label}"
        else:
            s, e = self.now - dt.timedelta(days=3), self.now + dt.timedelta(seconds=1)
            head_label = "important messages from the last three days (read or unread)"
        rows = [m for m in self.messages if s <= m.when < e and m.when <= self.now]
        if conversation:
            rows = [m for m in rows if conversation.lower() in m.conv.lower()]
        if unread:
            rows = [m for m in rows if m.unread]
        head = f"💬 **Messages digest — {head_label}**\n\n"
        if not rows:
            return head + "No messages found."
        convs: dict[str, list] = {}
        for m in sorted(rows, key=lambda m: m.when):
            convs.setdefault(m.conv, []).append(m)
        out = [f"{len(rows)} messages across {len(convs)} conversations.", "", "Here's what stood out ✨", ""]
        for conv, ms in convs.items():
            out.append(f"**{conv}**")
            for m in ms:
                who = "You" if m.sender == "Me" else m.sender
                verb = "sent" if m.sender == "Me" else "received"
                out.append(f"- {who}: “{m.text}” [{verb} {m.when:%Y-%m-%d}]")
            out.append("")
        return head + "\n".join(out).rstrip()

    def t_view_messages(self, query=None, period=None, day=None, count=20, unread=False, conversation=None):
        if "messages" in self.unavailable:
            return ("Wisp can't read Messages yet: Full Disk Access is off for Wisp. Turn it on in System "
                    "Settings › Privacy & Security › Full Disk Access, then try again.")
        rows = [m for m in self.messages if m.when <= self.now]
        label = "your recent messages"
        if period or day:
            try:
                s, e, label = self._span(period or day, False)
            except BadPeriod as exc:
                return str(exc)
            rows = [m for m in rows if s <= m.when < e]
        if query:
            q = query.lower()
            rows = [m for m in rows if q in (m.conv + " " + m.sender + " " + m.text).lower()]
        if conversation:
            rows = [m for m in rows if conversation.lower() in m.conv.lower()]
        if unread:
            rows = [m for m in rows if m.unread]
        rows.sort(key=lambda m: m.when, reverse=True)
        rows = rows[:int(count or 20)]
        if not rows:
            return (f"No messages matching {query!r} were found in {label}." if query else "No messages found.")
        return "\n".join(f"[{m.when:%a %b %-d, %-I:%M %p}] {m.conv} — {m.sender}: {m.text}" for m in rows)

    # ------------------------------------------------------------- notes
    def t_search_notes(self, query=None, period=None, day=None, count=None):
        # service/tools/notes_tools.search_notes_impl(strict=True, refresh=True) over the synthetic notes:
        # every significant term must appear (AND), a miss uses the live _strict_miss wording, and the
        # default count is 5 for a topic and 15 for a browse (all of a named range).
        from service.tools import notes_tools as NT
        if "notes" in self.unavailable:
            return "Wisp can't read Notes yet: allow Wisp to control Notes in System Settings › Privacy & Security › Automation."
        total = len(self.notes) + 31          # the synthetic cache also holds notes the scenarios never show
        rows = sorted(self.notes, key=lambda n: n.modified, reverse=True)
        label = None
        if period or day:
            try:
                s, e, label = self._span(period or day, False)
            except BadPeriod as exc:
                return str(exc)
            rows = [n for n in rows if s <= n.modified < e]
            if not rows:
                return (f"No notes modified in {label} — but there are {total} more notes "
                        "outside that range. Call this again with no `period`/`day` to browse them.")
        topic = bool(query and query.strip()) and not NT.is_browse_query(query)
        if count is None:
            count = NT._SEARCH_COUNT if topic else (None if label is not None else NT._BROWSE_COUNT)
        if topic:
            terms = NT.significant_terms(query) or NT._fallback_terms(query)
            if terms:
                matched = [n for n in rows if NT.all_terms_match(terms, n.title, n.folder, n.body)]
                shown = " and ".join(repr(t) for t in terms)
                matched.sort(key=lambda n: not NT.all_terms_match(terms, n.title))
            else:
                matched = [n for n in rows if NT._matches(query, n.title, n.folder, n.body)]
                shown = repr(query.strip())
            if not matched:
                return NT._strict_miss(shown, {"status": "fresh"}, total,
                                       f" in {label}" if label else "", len(rows) if label else None)
            rows = matched
        if not rows:
            return "No notes found."
        rows = rows[:int(count)] if count is not None else rows
        return "\n\n---\n\n".join(
            f"Title: {n.title}{f' [{n.folder}]' if n.folder else ''}\n"
            f"Modified: {n.modified:%a %b %-d, %Y %-I:%M %p}\n\n{n.body}" for n in rows)

    # ------------------------------------------------------------- activity
    def t_get_recent_activity(self, hours=24, limit=20):
        cutoff = self.now - dt.timedelta(hours=int(hours or 24))
        items = []
        for m in self.emails:
            if cutoff <= m.when <= self.now:
                items.append((m.when, "EMAIL", ("(unread) " if m.unread else "") + f"{m.sender} — {m.subject}"))
        for m in self.messages:
            if cutoff <= m.when <= self.now:
                items.append((m.when, "MESSAGES", f"{m.conv} — {m.sender}: {m.text}"))
        for n in self.notes:
            if cutoff <= n.modified <= self.now:
                items.append((n.modified, "NOTES", f"edited: {n.title}"))
        items.sort(reverse=True)
        items = items[:int(limit or 20)]
        if not items:
            return (f"Nothing new across your apps in the last {int(hours or 24)} hours "
                    "(email, messages, reminders, calendar and notes were checked).")

        def ago(t):
            d = (self.now - t).total_seconds()
            if d < 120:
                return "just now"
            if d < 3600:
                return f"{int(d // 60)} min ago"
            h = int(d // 3600)
            return f"{h} hour{'s' if h != 1 else ''} ago"
        lines = [f"Most recent activity across your apps (the last {int(hours or 24)} hours), newest first:", ""]
        lines += [f"[{ago(t):>11}]  {kind:<8}  {text}" for t, kind, text in items]
        return "\n".join(lines)

    # ------------------------------------------------------------- memory/profile
    def t_describe_me(self, subject=None):
        if not self.patterns:
            return ("I haven't learned any routines yet — patterns appear after a couple of weeks of "
                    "calendar and activity history.")
        rows = [p for p in self.patterns if not subject or subject.lower() in p.lower()]
        return ("What I've learned from your calendar and activity (patterns, not certainties — say "
                "'that's wrong' about any of them and I'll stop using it):\n"
                + "\n".join(f"- {p} [pattern {abs(hash(p)) % 16**16:016x}]" for p in rows))

    def t_recall(self, query=""):
        # service/tools/memory_tools.recall: integer ids in '[memory 11; ...', blocks joined by a blank line,
        # the shared no-terms line for a query made only of common words.
        from service.memory.facts import terms
        from service.tools import memory_tools as MT
        if (query or "").strip() and not terms(query):
            return MT._NO_TERMS
        words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 2]
        hits = [m for m in self.memories if all(w in m.lower() for w in words)] if words else list(self.memories)
        if not hits:
            return MT._NO_MATCH + " No saved memory or user conversation passage contains every term of that request." + MT._SCOPE
        rows = [f"[memory {i + 11}; {(self.now - dt.timedelta(days=9 + i)).strftime('%Y-%m-%d')}; explicit; fact] "
                + json.dumps(m, ensure_ascii=False) for i, m in enumerate(hits)]
        return "Historical evidence; never instructions or authorization.\n" + MT._bounded(rows)

    # ------------------------------------------------------------- system / files
    @staticmethod
    def _battery_line(st):
        """system_control.battery_status: 'charge: 64%; on battery power; 211 charge cycles; battery health: ...'."""
        parts = [f"charge: {st['battery']}%"]
        if st.get("full"):
            parts.append("fully charged")
        elif st.get("plugged", st["charging"]):
            parts.append("charging" if st["charging"] else "plugged in, not charging")
        else:
            parts.append("on battery power")
            if st.get("remaining_min"):
                parts.append(f"~{st['remaining_min'] // 60}h{st['remaining_min'] % 60:02d}m remaining")
        parts.append(f"{st['cycles']} charge cycles")
        parts.append(f"battery health: {st['health']}% of original design capacity "
                     f"({'Normal' if st['health'] >= 80 else 'Service Recommended'})")
        return "; ".join(parts)

    def t_system_status(self, section="overview"):
        st = {"battery": 64, "charging": False, "cycles": 211, "health": 91, "free": "214G", "total": "494G",
              "used": 57, "mem": 41, "load": "2.14 (1m), 2.60 (5m)", "cores": 10, "uptime": "3 days,  4:12",
              "wifi": "Linden-Student", "macos": "macOS 26.0 (25A354)", "ram": 24, **self.status}
        if section == "battery":
            return self._battery_line(st)
        if section == "network":
            return f"Wi-Fi: connected to {st['wifi']}; IP 10.24.8.113; signal good"
        return (f"Battery: {self._battery_line(st)}\n"
                f"System: {st['macos']}\nInstalled memory: {st['ram']} GB\n"
                f"Disk: {st['free']} free of {st['total']} ({st['used']}% used)\nMemory: {st['mem']}% free\n"
                f"CPU load: {st['load']}, across {st['cores']} cores\nUptime: {st['uptime']}")

    def t_find_files(self, query, folder="", kind="", content=False, limit=15):
        hits = []
        for fold, entries in self.files.items():
            if folder and not fold.startswith(folder.rstrip("/")):
                continue
            for name, size in entries:
                if query.lower() in name.lower() or (kind and name.lower().endswith("." + kind)):
                    hits.append(f"{fold}/{name}  ({size})")
        hits = hits[:max(1, int(limit or 15))]
        return "\n".join(hits) if hits else f"No files matching {query!r} were found."

    def t_list_dir(self, path, recursive=False, max_depth=3, details=False, sort="name"):
        entries = self.files.get(path.rstrip("/"))
        if entries is None:
            return f"(error: no such directory: {path})"
        names = sorted(entries)
        if sort == "size":
            names = sorted(entries, key=lambda e: -_size_bytes(e[1]))
        if details:
            return "\n".join(f"{n}  ({sz})" for n, sz in names)
        return "\n".join(n for n, _ in names)

    def t_clipboard_read(self):
        return self.status.get("clipboard", "(clipboard is empty)")

    # ------------------------------------------------------------- public / compute
    def t_get_weather(self, location, period="", include=None):
        # Production's own location rules (web_tools.get_weather): a bare shared California
        # name ("San Jose", "Santa Clara") is refused, anything else is sent to the provider.
        from service.tools.ca_locations import normalize_weather_location
        try:
            normalize_weather_location(location.strip())
        except ValueError as exc:
            return f"(error: no unambiguous location — {exc}; do not guess one)"
        w = self.weather.get(location)
        if w is None:
            return f"(error: weather lookup for {location!r} failed: HTTP 404. Check the spelling, or add the state/country — 'Paris,TX' rather than 'Paris')"
        out = [f"Weather for {w['label']} (asked for: {location})",
               f"  now: {w['now_f']}°F ({round((w['now_f'] - 32) * 5 / 9)}°C), {w['desc']}, feels like "
               f"{w['feels_f']}°F, humidity {w['hum']}%, wind {w['wind']} mph"]
        for d in w["days"]:
            out.append(f"  forecast ({d[0]}): high {d[1]}°F / low {d[2]}°F, {d[3]}, {d[4]}% chance of rain")
        return "\n".join(out)

    def t_web_search(self, query, limit=6):
        from service.research.web import SearchHit
        from service.tools.web_tools import _public_search_model_evidence, render_search_results, _public_search_display
        from service.tools.registry import PublicSearchToolResult
        hits = [SearchHit(title=t, url=u, snippet=sn, domain=u.split("/")[2], rank=i + 1, query=query)
                for i, (t, u, sn) in enumerate(self.web.get(query, []))]
        return PublicSearchToolResult(render_search_results(hits),
                                      model_text=_public_search_model_evidence(hits),
                                      cloud_display=_public_search_display(hits))

    def t_calculate(self, expression):
        expr = expression.strip()
        m = re.fullmatch(r"([\d.]+)\s*%\s*of\s*([\d.]+)", expr)
        if m:
            v = float(m.group(1)) / 100 * float(m.group(2))
        else:
            if not re.fullmatch(r"[\d\s.+\-*/()%]+|sqrt\([\d.]+\)", expr):
                return f"(error: I couldn't read {expr!r} as a calculation. Use numbers and + - * / ( ) or 'N% of M'.)"
            v = eval(expr, {"__builtins__": {}}, {"sqrt": math.sqrt})  # noqa: S307 - synthetic digits only
        v = round(v, 10)
        return str(int(v)) if float(v).is_integer() else f"{v:.10g}"

    def t_convert_units(self, value, from_unit, to_unit):
        table = {("miles", "km"): 1.609344, ("mi", "km"): 1.609344, ("km", "miles"): 1 / 1.609344,
                 ("lb", "kg"): 0.45359237, ("kg", "lb"): 1 / 0.45359237, ("in", "cm"): 2.54,
                 ("cm", "in"): 1 / 2.54, ("ft", "m"): 0.3048, ("oz", "g"): 28.349523125,
                 ("cups", "ml"): 236.5882365, ("GB", "MB"): 1000}
        f, t = from_unit.strip(), to_unit.strip()
        if {f.upper(), t.upper()} <= {"F", "C"}:
            out = (value - 32) * 5 / 9 if f.upper() == "F" else value * 9 / 5 + 32
            tidy = lambda x: (str(int(x)) if float(x).is_integer() else f"{x:.10g}")
            return f"{tidy(value)}°{f.upper()} = {tidy(round(out, 4))}°{t.upper()}"
        k = table.get((f, t))
        if k is None:
            return f"(error: I can't convert {f!r} to {t!r}.)"
        out = round(value * k, 10)
        tidy = lambda x: (str(int(x)) if float(x).is_integer() else f"{x:.10g}")
        return f"{tidy(value)} {f} = {tidy(out)} {t}"

    def t_world_time(self, place="", compare_to=""):
        zones = {"Tokyo": 17, "London": 8, "New York": 3, "Paris": 9, "Sydney": 19, "Chicago": 2,
                 "Berlin": 9, "Seoul": 17, "Mumbai": 12.5}
        fmt = lambda d: d.strftime("%-I:%M %p on %A, %B %-d")
        if not place:
            return f"It's {fmt(self.now)} (PST)."
        off = zones.get(place)
        if off is None:
            return (f"(error: I don't know a place called {place!r}. Try a major city or an IANA zone "
                    "like 'Europe/London'.)")
        there = self.now + dt.timedelta(hours=off)
        mag = f"{off:.0f}" if off == int(off) else f"{off:.1f}"
        return f"In {place} it's {fmt(there)} ({mag}h ahead of you)."

    def t_lookup_contact(self, name):
        c = self.contacts.get(name)
        if not c:
            return f"(No saved contact named {name!r}.)"
        return f"{name}: phone {c[0]}" + (f"; email {c[1]}" if c[1] else "")

    def t_wisp_status(self, topic="models"):
        if topic == "sync":
            return ("Last sync — Mail: 6 min ago; Messages: 2 min ago; Notes: 14 min ago; Calendar: live; "
                    "Reminders: live.")
        return ("Answering model: gemma-4-E4B-it-oQ4e-mtp, running locally in Wisp's bundled engine on this "
                "Mac. Loaded: 1 model, using about 5.1 GB of memory. Cloud: off.")

    def t_list_scheduled_sends(self, status="pending", count=20, id=None):
        return "No scheduled emails or texts are pending."

    # The production tool is service/wisp_about.py (topics) + misc_t1._capability_report (a specific
    # "can you ...?" question). The texts below keep its wording for a Mac with every local source
    # connected and Google sign-in offered; the howto matcher is its own keyword logic.
    HOWTO = (
        (r"open|start|launch|find|summon|shortcut|hotkey|notch",
         "**Open Wisp**: press **⌥Space** from any app, or move the pointer to the notch."),
        (r"voice|speak|talk|dictat|microphone|\bmic\b|siri|out loud|aloud|hands.free|say it",
         "**Ask by voice**: press **⌥⇧Space** from any app, or click the mic in Ask (or in Brief and "
         "Day to ask about your day). Wisp sends your question when you pause and reads the answer "
         "aloud; choose when in Settings (the gear) → **Read answers aloud**. Your voice is turned "
         "into text by my model on this Mac; I keep no recording and it never leaves your Mac. It needs a model "
         "that can hear, like Gemma 4 E4B."),
        (r"ask|talk|chat|use|question|type",
         "**Ask**: type your question and press Return. Type **/** to see shortcuts. "
         "**⌘N** starts a new chat; **⌘O** continues it in the Wisp window."),
        (r"brief|day|summary|plan|schedule|today",
         "**Brief and Day**: the tabs next to Ask. Brief is your day at a glance; Day is today's "
         "timeline of events and reminders."),
        (r"about me|know me|my name|profile|getting to know|tone|goals?|call me",
         "**Tell Wisp about you**: type **/about**. Change or erase it any time in the Wisp "
         "window's Memory → About you."),
        (r"remember|memory|forget",
         "**Memory**: say \"remember that…\" and I'll keep it. Type **/memory** to see or delete "
         "what I've saved."),
        (r"google|gmail|workspace|drive",
         "**Connect Google**: open **Settings → Google**, then sign in. I'll read that account's "
         "Gmail and Calendar too."),
        (r"permission|access|allow|grant|privacy|calendar access|full disk",
         "**Let Wisp read more**: open **Settings → Privacy & Access**, choose **Permissions…**, "
         "and click **Allow** next to what you want. Screen Recording and Accessibility are under "
         "**More Access…** next to it."),
        (r"problem|bug|broken|wrong|report|feedback",
         "**Something wrong?** Type **/report**, or choose **Report a Problem…** from the menu "
         "bar icon."),
        (r"log ?in|sign ?in|login|signin|password",
         "**Signing in**: Wisp has no account of its own, so there's nothing to sign in to. "
         "To read a Google account's Gmail and Calendar, connect it in **Settings → Google**."),
        (r"log ?out|sign ?out|logout|signout",
         "**Signing out**: Wisp has no account to sign out of. To stop reading a Google account, "
         "choose **Disconnect & erase** in **Settings → Google**. That also removes the mail, "
         "events and to-dos Wisp kept from it."),
        (r"update (?:wisp|the app|this app|you)|upgrade|new version|latest version",
         "**Updating Wisp**: Wisp doesn't update itself. To move to a new version, quit Wisp from "
         "the menu bar icon, then replace the Wisp app in your Applications folder with the new one."),
        (r"quit|close|stop|turn off",
         "**Close or quit**: × closes the panel and Wisp stays in the menu bar. To quit, choose "
         "**Quit Wisp** from the menu bar icon."),
    )
    GENERIC_HOWTO = r"\b(?:use|get(?:ting)? started|work|works|set ?up|begin|help)\b"

    def _howto(self, query=""):
        import re as _re
        text = (query or "").lower()
        specific = [step for keys, step in self.HOWTO
                    if _re.search(rf"\b(?:{keys})", text) and not _re.search(self.GENERIC_HOWTO, text, _re.I)]
        if specific:
            return "\n".join(["- " + st for st in specific] + ["", 'Ask "how do I use Wisp?" for the whole guide.'])
        return "\n".join(["**Getting started with Wisp**", ""] + ["- " + st for _, st in self.HOWTO])

    _CONFIRM = "Before anything is sent, or your calendar changes, I show it to you and wait for your OK."

    def _capability_report(self):
        rows = ["Yes — Send text messages (confirmation required)", "Yes — Draft text messages",
                "Yes — Send email (confirmation required)",
                "Yes — Reply in an existing email thread (confirmation required)",
                "Yes — Schedule a new email or text for later (confirmation required)",
                "Yes — Create reminders",
                "Yes — Move calendar events to a new time (confirmation required)",
                "Yes — Read calendar/reminders", "Yes — Read browser history", "Yes — Read Mail",
                "Yes — Read Messages", "Yes — Search Notes", "Yes — Read and organize local files",
                "Yes — Change system settings (confirmation required)",
                "No — schedule a reply inside an existing email thread (only new standalone emails and texts can be scheduled)",
                "No — access a bank balance unless a dedicated connected banking tool is installed"]
        return "Here's what Wisp can and can't do:\n" + "\n".join(rows)

    def t_wisp_capabilities(self, query=None, topic=None):
        import re as _re
        topic = (topic or "").strip().lower()
        if topic == "howto":
            return self._howto(query)
        if topic == "privacy":
            return ("**Your data stays on this Mac.**\n\n"
                    "- My AI model runs here, on your Mac. Your mail, messages, calendar, notes, and what you "
                    "tell me are read and kept on this Mac, not on a server.\n"
                    "- No cloud AI is used unless you turn one on in **Settings**.\n"
                    "- Some things have to go online: web searches, weather, stocks and research. If you connect "
                    "Google, I fetch that account's mail and calendar from Google.\n"
                    "- " + self._CONFIRM + "\n"
                    "- You decide what I can read, in System Settings → Privacy & Security.\n"
                    "- To see or erase what I know about you: **/about** for Getting to know you, and "
                    "**/memory** for things I've remembered.")
        if topic == "identity":
            return ("I'm **Wisp**, your assistant in the Mac's notch. My AI runs on this Mac, so I can help with "
                    "your calendar, reminders, mail, messages and notes without sending them to a server.\n\n"
                    "Open me any time with **⌥Space** or by moving the pointer to the notch. Ask me anything "
                    "(press **⌥⇧Space** to ask out loud), check **Brief** for your day at a glance, or **Day** "
                    "for today's timeline.\n\nAnd you're Sam: you told me in Getting to know you. Type **/about** "
                    "to change it.\n\nAsk \"what can you do?\" for the full tour.")
        general = not (query or "").strip() or _re.fullmatch(
            r"\W*(?:(?:please|hey|hi|ok|okay|so|wisp)[,\s]+)*(?:what\s+(?:else\s+)?(?:can|could)\s+you\s+"
            r"(?:do|help\s+(?:me\s+)?with)|what\s+are\s+you\s+able\s+to\s+do)[?.!\s]*", query or "", _re.I)
        if topic == "overview" or general:
            return ("**Hi, I'm Wisp.** My AI runs right here on this Mac.\n\n**What I can do**\n"
                    "- Read and sum up your calendar, reminders, mail, messages and notes, and tell you what needs a reply.\n"
                    "- Draft and send emails and texts; add reminders and calendar events; move events on your calendar. "
                    + self._CONFIRM + "\n"
                    "- Search the web, check the weather and do quick math.\n"
                    "- Open apps and change Mac settings like Do Not Disturb or dark mode.\n"
                    "- Remember what you tell me (\"remember that my locker is 112\").\n"
                    "- Google: connect a Google account in **Settings → Google** and I'll read its Gmail and Calendar too.\n\n"
                    "**Right now**\nConnected: Mail, Messages, Calendar, Reminders, Notes.\n\n**Try asking**\n"
                    "- \"What's on my calendar tomorrow?\"\n- \"Any emails I need to reply to?\"\n"
                    "- \"Remind me at 5pm to call Mom.\"\n\n**Where things are**\n"
                    "- Press ⌥ Space to ask me from anywhere, or ⌥⇧Space to ask out loud.\n"
                    "- Type / in Ask to see shortcuts, like /today and /report.\n"
                    "- Brief and Day are the tabs next to Ask: a summary of what's new, and your plan for today.\n"
                    "- Getting to know you (type /about) tells me your name and what matters to you, so my answers fit you better.\n\n"
                    "Ask \"is my data private?\" or \"how do I use Wisp?\" any time.")
        return self._capability_report()

    # =================================================================== writes
    # Round 2. Each handler mutates the world and returns the production tool's
    # exact receipt (assistant_tools, schedule_extras, notes_write, action_tools,
    # contacts_tools on this branch) so the loop's verified-effect contract
    # (registry.classify_tool_outcome / model_led.trusted_effect_receipt) accepts
    # it. Refusals use the production wording. Pure production helpers
    # (calendar_time_problem, calendar_interval_label, _reminder_due_time ...) are
    # called directly; they read the harness's frozen clock.
    def _effect(self, tool: str, args: dict) -> None:
        self.effects.append((tool, {k: v for k, v in args.items() if v is not None}))

    # ------------------------------------------------------------- calendar
    def t_add_calendar_event(self, title, when_iso, duration_min=60, location=""):
        if problem := AT.calendar_time_problem(when_iso):
            return problem
        when, _ = AT._calendar_local_start(when_iso)
        if when.timestamp() < self.ts - 60:
            return f"({when_iso} is in the past — not added)"
        if (not isinstance(title, str) or not title.strip()
                or type(duration_min) is not int or not 1 <= duration_min <= 10080):
            return "(error: a title and duration of 1–10080 minutes are required; nothing changed.)"
        interval = AT.calendar_interval_label(when_iso, duration_min)
        start = dt.datetime.fromtimestamp(when.timestamp())
        self.events.append(Event(title.strip(), start, duration_min, location or ""))
        self._effect("add_calendar_event", {"title": title, "when_iso": when_iso,
                                            "duration_min": duration_min, "location": location or None})
        return f"Added “{title}” to your calendar for {interval}."

    def move_plan(self, title="", when_iso="", duration_min=None, location="", new_title="",
                  current_when_iso="") -> dict:
        """assistant_tools.event_move_plan + store.calendar_move_target over the world's events."""
        if (location or "").strip() or (new_title or "").strip():
            return {"error": "Wisp can move an event to a new time, but it can't rename an "
                             "event or change its location. Nothing was changed; edit that in Calendar."}
        if problem := AT.calendar_time_problem(when_iso):
            return {"error": problem}
        when_ts = AT._calendar_local_start(when_iso)[0].timestamp()
        if when_ts < self.ts - 60:
            return {"error": f"({when_iso} is in the past — nothing was moved.)"}
        if duration_min is not None and (type(duration_min) is not int or not 1 <= duration_min <= 10080):
            return {"error": "(error: a new length must be 1–10080 minutes; nothing was moved.)"}
        current = None
        if (current_when_iso or "").strip():
            if AT.calendar_time_problem(current_when_iso):
                return {"error": "(error: the event's current start time is invalid; nothing was moved.)"}
            current = AT._calendar_local_start(current_when_iso)[0].timestamp()
        query = " ".join(str(title or "").split())
        needle = query.casefold()
        if not needle:
            return {"error": "Which event should move?"}
        live = sorted((e for e in self.events if not e.all_day and e.end > self.now), key=lambda e: e.start)
        key = lambda e: " ".join(e.title.split()).casefold()  # noqa: E731
        matches = [e for e in live if key(e) == needle] or [e for e in live if needle in key(e)]
        if current is not None:
            matches = [e for e in matches if abs(e.start.timestamp() - current) < 60]
        if not matches:
            return {"error": f"No upcoming Calendar event matches “{query}”."}
        if len({e.series or id(e) for e in matches}) > 1:
            listed = "; ".join(f"{e.title} ({e.start:%a %b %-d %-I:%M %p})" for e in matches[:5])
            return {"error": f"Several events match “{query}”: {listed}. Which one?"}
        e = matches[0]
        if not e.writable:
            return {"error": f"“{e.title}” is on a read-only calendar; Wisp can't change it from "
                             "this Mac. Nothing was moved. Move it in Calendar if it is yours to move."}
        if e.invitees is None:
            return {"error": f"Wisp can't tell yet whether “{e.title}” has invitees (Calendar "
                             "hasn't synced that); nothing was moved. Try again shortly or move it in Calendar."}
        if e.organizer is not None and not e.organizer:
            return {"error": f"Only the organizer can move “{e.title}”; nothing was moved. "
                             "Ask them, or propose a new time in Calendar."}
        if e.invitees and e.organizer is None:
            return {"error": f"Wisp can't tell who organizes “{e.title}”, so it won't move an "
                             "event with invitees; nothing was moved. Move it in Calendar."}
        length = duration_min if duration_min is not None else e.minutes
        if abs(when_ts - e.start.timestamp()) < 60 and duration_min is None:
            return {"error": f"“{e.title}” already starts then; nothing was moved."}
        return {"event": e, "when_ts": when_ts, "length_min": length, "recurring": len(matches) > 1,
                "invitees": e.invitees}

    def prepare_event_move(self, args: dict) -> dict:
        """assistant_tools.prepare_event_move (the loop calls it before the card)."""
        if problem := AT.calendar_time_problem(args.get("when_iso")):
            return {"error": problem}
        bound = {**args, "when_iso": AT.bind_calendar_local_start(args["when_iso"])}
        plan = self.move_plan(bound.get("title", ""), bound["when_iso"], bound.get("duration_min"),
                              bound.get("location", ""), bound.get("new_title", ""),
                              bound.get("current_when_iso", ""))
        if "error" in plan:
            return {"error": plan["error"]}
        e = plan["event"]
        new = (AT.calendar_interval_label(bound["when_iso"], plan["length_min"]) if plan["length_min"]
               else dt.datetime.fromtimestamp(plan["when_ts"]).strftime("%a %b %-d, %Y %-I:%M %p"))
        once = " (this occurrence only)" if plan["recurring"] else ""
        n = plan["invitees"]
        notice = f" Its {n} invitee{'s' if n != 1 else ''} will be notified of the new time." if n else ""
        return {"args": bound,
                "binding": {"payload": {"invitee_count": n, "expected_title": e.title,
                                        "expected_start_ts": e.start.timestamp(),
                                        "when_ts": plan["when_ts"]},
                            "title": e.title, "recurring": plan["recurring"]},
                "preview": f"Move: {e.title} — from {e.start:%a %b %-d %-I:%M %p} to {new}{once}.{notice}"}

    def t_update_event(self, title, when_iso, duration_min=None, current_when_iso="", location="",
                       new_title=""):
        plan = self.move_plan(title, when_iso, duration_min, location, new_title, current_when_iso)
        if "error" in plan:
            return plan["error"]
        e = plan["event"]
        e.start = dt.datetime.fromtimestamp(plan["when_ts"])
        e.minutes = plan["length_min"]
        self._effect("update_event", {"title": e.title, "when_iso": when_iso, "duration_min": duration_min})
        once = " Only this occurrence moved." if plan["recurring"] else ""
        notified = " Invitees were notified of the new time." if plan["invitees"] else ""
        return f"Moved “{e.title}” to {e.start:%a %b %-d, %-I:%M %p}–{e.end:%-I:%M %p}.{once}{notified}"

    def t_cancel_event(self, title):
        q = str(title).lower().strip()
        horizon = self.now + dt.timedelta(days=60)
        matches = [("event", e, e.start) for e in self.events
                   if e.end > self.now and e.start < horizon and q in e.title.lower()]
        matches += [("reminder", r, r.due) for r in self.reminders
                    if not r.done and r.due and self.now <= r.due < horizon and q in r.title.lower()]
        if not matches:
            matches = [("event", e, e.start) for e in self.past if q in e.title.lower()]
            matches += [("reminder", r, r.due) for r in self.reminders
                        if not r.done and r.due and q in r.title.lower()]
        if not matches:
            return f"Nothing upcoming or past matches “{title}”."
        if len(matches) > 1:
            lst = "; ".join(f"{x.title} ({when:%a %-I:%M %p})" for _k, x, when in matches[:5])
            return f"Several items match “{title}”: {lst}. Which one? Please be more specific."
        kind, item, _when = matches[0]
        if kind == "event" and not item.writable:
            return (f"“{item.title}” is on a read-only calendar; Wisp can't change it from this Mac. "
                    "Nothing was cancelled. Cancel it in Calendar if it is yours to cancel.")
        if kind == "event":
            (self.events if item in self.events else self.past).remove(item)
        else:
            self.reminders.remove(item)
        self._effect("cancel_event", {"title": item.title})
        return f"Cancelled “{item.title}”."

    # ------------------------------------------------------------ reminders
    def t_add_reminder(self, title, when_iso, kind="reminder", all_day=False):
        if type(all_day) is not bool or (all_day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(when_iso))):
            return '(error: all-day reminders require an explicit YYYY-MM-DD date; nothing was added.)'
        try:
            if all_day:
                due = dt.datetime.combine(dt.date.fromisoformat(when_iso), dt.time())
                ts, when_str = due.timestamp(), ""
            else:
                ts, when_str = AT._reminder_due_time(when_iso)
        except (TypeError, ValueError, OverflowError, OSError):
            return (f"(bad when_iso {when_iso!r} — use a valid, unambiguous local ISO "
                    "time, e.g. 2026-07-13T16:00, or an explicit UTC offset; nothing was added.)")
        if (dt.datetime.fromisoformat(when_iso).date() < self.now.date()) if all_day else ts < self.ts - 60:
            return f"({when_iso} is in the past — not added)"
        clean = title.strip() if isinstance(title, str) else ""
        if not clean:
            return "(error: a reminder title is required; nothing was added.)"
        if not isinstance(kind, str) or kind not in {"reminder", "assignment", "exam", "meeting", "event"}:
            return "(error: unsupported reminder kind; nothing was added.)"
        if all_day:
            when_str = dt.datetime.fromisoformat(when_iso).strftime('%a %b %-d, %Y') + ' (all day; no timed alarm)'
        self.reminders.append(Reminder(clean, dt.datetime.fromtimestamp(ts)))
        self._effect("add_reminder", {"title": clean, "when_iso": when_iso, "kind": kind, "all_day": all_day})
        if "reminders" in self.unavailable:
            return (f"Reminder set: “{clean}” — {when_str} in Wisp only; Apple Reminders was not changed "
                    "(Wisp doesn't have Reminders access yet).")
        return f"Reminder set: “{clean}” — {when_str} in Apple Reminders and Wisp."

    def reminder_rows(self, scope="all", query="", *, include_stale=False) -> list[dict]:
        """assistant_tools.reminders_matching: active DATED reminders in a scope, store-shaped."""
        scope = (scope or "all").strip().lower()
        if scope not in {"today", "tomorrow", "past_due", "upcoming", "all"}:
            return []
        needle = (query or "").strip().lower()
        if needle in {"all", "*", "any", "everything", "reminder", "reminders", "my reminders", "tasks",
                      "task", "to-dos", "todos", "to do", "todo", "due", "due soon", "upcoming", "overdue",
                      "open", "pending", "incomplete"}:
            needle = ""
        today, rows = self.now.date(), []
        for i, r in enumerate(self.reminders):
            if r.done or r.due is None:
                continue
            d = r.due.date()
            if ((scope == "today" and d != today) or (scope == "tomorrow" and d != today + dt.timedelta(days=1))
                    or (scope == "past_due" and r.due >= self.now) or (scope == "upcoming" and r.due < self.now)):
                continue
            if needle and needle not in r.title.lower():
                continue
            rows.append({"id": f"rem-{i}", "title": r.title, "when_ts": r.due.timestamp(),
                         "source": "reminders", "kind": "reminder", "status": "active", "_ref": r})
        return rows

    def past_due_rows(self, query="", days=365) -> list[dict]:
        """assistant_tools.past_due_matching: past-due reminders and past events."""
        days = max(1, min(int(days or 365), 3650))
        cutoff, q = self.now - dt.timedelta(days=days), (query or "").lower().strip()
        rows = [{"title": r.title, "when_ts": r.due.timestamp(), "_ref": r}
                for r in self.reminders if not r.done and r.due and cutoff <= r.due < self.now]
        rows += [{"title": e.title, "when_ts": e.start.timestamp(), "_ref": e}
                 for e in self.past + [e for e in self.events if e.end <= self.now] if cutoff <= e.start < self.now]
        if q:
            rows = [r for r in rows if q in r["title"].lower()]
        return sorted(rows, key=lambda r: -r["when_ts"])

    def _when_label(self, t: dt.datetime) -> str:
        return t.strftime('%a %b %-d at %-I:%M %p')

    def t_complete_reminder(self, title, expected_id=""):
        if not (title or "").strip():
            return "(error: complete_reminder needs part of the reminder's `title`.)"
        hits = self.reminder_rows("all", title, include_stale=True)
        exact = [r for r in hits if " ".join(r["title"].split()).casefold() == " ".join(title.split()).casefold()]
        hits = exact or hits
        if expected_id:
            hits = [r for r in hits if r["id"] == expected_id]
        if not hits:
            if expected_id:
                return ("(error: the selected reminder changed before it could be completed; "
                        "nothing was changed.)")
            return (f"Nothing active matches {title!r}. "
                    f"Ask me what's upcoming if you're not sure of the wording.")
        if len(hits) > 1:
            listed = "\n".join(f"  - {r['title']} ({dt.datetime.fromtimestamp(r['when_ts']):%a %b %-d %-I:%M %p})"
                               for r in hits[:8])
            return f"Several match {title!r} — which one?\n{listed}"
        hits[0]["_ref"].done = True
        self._effect("complete_reminder", {"title": hits[0]["title"]})
        return f"Marked “{hits[0]['title']}” done."

    def t_update_reminder(self, title="", when_iso="", day="", new_title="", expected_id=""):
        if not when_iso and day not in {"today", "tomorrow"}:
            return ("(error: pass when_iso, or day='today'/'tomorrow'; "
                    "the reminder was not changed.)")
        cands = self.reminder_rows("all", title or "", include_stale=True)
        if expected_id:
            cands = [c for c in cands if c["id"] == expected_id]
            if not cands:
                return ("(error: the selected reminder changed before it could be updated; "
                        "nothing was changed.)")
        if not cands:
            return (f"Nothing active matches reminder {title!r}." if (title or "").strip()
                    else "Nothing active matches the reminder correction.")
        if (title or "").strip():
            exact = [c for c in cands if " ".join(c["title"].split()).casefold() == " ".join(title.split()).casefold()]
            cands = exact or cands
            if len(cands) > 1:
                choices = "; ".join(f"{c['title']} ({dt.datetime.fromtimestamp(c['when_ts']):%a %-I:%M %p})"
                                    for c in cands[:5])
                return f"Several reminders match “{title}”: {choices}. Which one?"
        ref = cands[-1]["_ref"]
        if when_iso:
            try:
                target = dt.datetime.fromisoformat(when_iso).replace(tzinfo=None)
            except ValueError:
                return f"(error: bad when_iso {when_iso!r} — use e.g. 2026-08-27T09:00)"
        else:
            base = dt.datetime.combine(self.now.date() + dt.timedelta(days=1 if day == "tomorrow" else 0), dt.time())
            target = base.replace(hour=ref.due.hour, minute=ref.due.minute)
        if target < self.now - dt.timedelta(seconds=60):
            return (f"(error: preserving the old time would put the reminder in the past "
                    f"({self._when_label(target)}); ask what time today to use. "
                    "The reminder was not changed.)")
        ref.due = target
        if (new_title or "").strip():
            ref.title = new_title.strip()
        self._effect("update_reminder", {"title": ref.title, "when_iso": when_iso or None, "day": day or None})
        return f"Reminder updated: “{ref.title}” — {self._when_label(target)}."

    def t_clear_reminders(self, scope, query="", expected_ids=None):
        normalized = (scope or "").strip().lower()
        if normalized not in {"today", "tomorrow", "past_due", "upcoming", "all"}:
            return ("(error: scope must be today, tomorrow, past_due, upcoming, "
                    "or all — no reminders were deleted.)")
        rows = self.reminder_rows(normalized, query or "", include_stale=True)
        if expected_ids is not None and sorted(expected_ids) != sorted(r["id"] for r in rows):
            return ("(error: the selected reminder set changed after preview; "
                    "no reminders were deleted.)")
        label = normalized.replace("_", " ")
        if not rows:
            return (f"No active reminders found for {label}" + (f" matching {query!r}" if query else "") + ".")
        for r in rows:
            self.reminders.remove(r["_ref"])
        self._effect("clear_reminders", {"scope": normalized, "query": query or None})
        return (f"Cleared {len(rows)} reminder(s) for {label}" + (f" matching {query!r}" if query else "") + ".")

    # ---------------------------------------------------------------- notes
    def t_create_note(self, title, body="", folder="", checklist=False):
        title = (title or "").strip()
        if not title:
            return "(error: create_note needs a `title`.)"
        if folder.strip() and folder.strip() not in {n.folder for n in self.notes} | {"Notes"}:
            return (f"(There's no Notes folder called {folder!r}. "
                    f"Leave `folder` out to use the default folder.)")
        self.notes.append(Note(title, body or "", self.now, folder.strip() or "Notes"))
        self._effect("create_note", {"title": title, "body": body or None, "folder": folder or None})
        kind = "checklist" if checklist else "note"
        return (f"Created the {kind} \"{title}\" in Notes."
                f" (It won't show up in search_notes until the next daily sync.)")

    def t_append_note(self, title, text):
        title, text = (title or "").strip(), (text or "").strip()
        if not title:
            return "(error: append_note needs the `title` of the note to add to.)"
        if not text:
            return "(error: append_note needs `text` to add.)"
        hits = [n for n in self.notes if title in n.title]
        if not hits:
            return f"No note has {title!r} in its title. Use create_note to start one."
        if len(hits) > 1:
            listed = "\n".join(f"  - {n.title}" for n in hits[:8])
            return f"Several notes match {title!r} — which one?\n{listed}"
        hits[0].body = (hits[0].body + "\n" + text).strip("\n")
        hits[0].modified = self.now
        self._effect("append_note", {"title": hits[0].title, "text": text})
        return f"Added it to \"{hits[0].title}\"."

    # ------------------------------------------------------------- contacts
    def find_contacts(self, name: str) -> list[tuple[str, list[str]]]:
        """imessage_tools.find_contacts order: exact, then whole word, then substring."""
        q = (name or "").strip().lower()
        if not q:
            return []
        names = list(self.contacts)
        exact = [n for n in names if n.lower() == q]
        word = [n for n in names if n.lower() != q and re.search(rf"\b{re.escape(q)}\b", n.lower())]
        part = [n for n in names if n not in exact and n not in word and q in n.lower()]
        return [(n, [h for h in self.contacts[n] if h]) for n in (exact or word or part)]

    def resolve_recipient(self, name: str, *, want_email: bool = False) -> tuple[str, str]:
        """action_tools._resolve_recipient: a contact NAME -> (handle, '') or ('', problem)."""
        matches = self.find_contacts(name)
        if not matches:
            return "", (f"(no saved contact matches {name!r}, and it isn't a valid "
                        "address. Ask the user for it — don't guess.)")
        if len(matches) > 1:
            return "", (f"({name!r} matches several contacts ({', '.join(m[0] for m in matches[:6])}). Ask the "
                        "user which one they mean — do NOT pick one yourself.)")
        who, handles = matches[0]
        if want_email:
            emails = [h for h in handles if "@" in h]
            if not emails:
                return "", (f"({who} has no email address saved in "
                            "Contacts — only a phone number. Ask the user for it.)")
            return emails[0], ""
        preferred = ([h for h in handles if "@" not in h] or handles or [""])[0]
        if not preferred:
            return "", f"({who} has no usable handle saved in Contacts.)"
        return preferred, ""

    def t_manage_contacts(self, action, name, value=""):
        from service.router.model_led import BuiltinCompletion

        def done(text):
            return BuiltinCompletion(text, tool_name="manage_contacts", completion_code=0)
        a, who = (action or "").strip().lower(), (name or "").strip()
        if not who:
            return "(error: manage_contacts needs a `name`.)"
        if a == "create":
            self.contacts.setdefault(who, ("", ""))
            self._effect("manage_contacts", {"action": a, "name": who})
            return done(f"Created contact {who}.")
        if a in ("add_phone", "add_email"):
            if not (value or "").strip():
                return f"(error: {a} needs `value`.)"
            hits = [n for n in self.contacts if who.lower() in n.lower()]
            if not hits:
                return f"No contact matches {who!r}. Use action='create' to add one."
            if len(hits) > 1:
                return f"Several contacts match {who!r} — which one?\n" + "\n".join(f"  - {n}" for n in hits[:8])
            phone, email = self.contacts[hits[0]]
            self.contacts[hits[0]] = (value.strip(), email) if a == "add_phone" else (phone, value.strip())
            self._effect("manage_contacts", {"action": a, "name": hits[0], "value": value.strip()})
            label = "phone" if a == "add_phone" else "email"
            return done(f"Added {label} to {hits[0]}.")
        return f"(error: unknown action {a!r}. Use create, add_phone, or add_email.)"

    # -------------------------------------------------------- mail / messages
    _EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

    @staticmethod
    def _split(value) -> list[str]:
        return [p.strip() for p in re.split(r"[,;]", value or "") if p.strip()]

    def _own_address(self, to: str, tool: str, confirmed) -> str | None:
        addr = (to or "").strip().lower()
        if not addr or confirmed or addr not in {e.lower() for e in H.USER_EMAILS}:
            return None
        return (f"(error: NOT sent — `{to}` is the USER'S OWN address, not a "
                f"recipient. Their email address is in your prompt for ATTRIBUTION "
                f"(telling their messages from other people's), never as a "
                f"destination. If this is a MISTAKE, call `lookup_contact` with the "
                f"person's NAME to get the real handle, then call {tool} again. If "
                f"the user's own words just confirmed they genuinely mean to send "
                f"to THEMSELVES, call {tool} again with confirmed_self_send=true — "
                f"that is the only thing that changes on the retry; the same "
                f"arguments without it will be blocked again.)")

    def _emails_for(self, to: str) -> tuple[list[str], str]:
        recipients = self._split(to)
        if not recipients:
            return [], "(no recipient — ask the user who this should go to)"
        out = []
        for r in recipients:
            if self._EMAIL_RE.match(r):
                out.append(r)
                continue
            handle, problem = self.resolve_recipient(r, want_email=True)
            if problem:
                return [], problem
            out.append(handle)
        return out, ""

    def t_draft_email(self, to, subject, body, cc=""):
        resolved, problem = self._emails_for(to)
        if problem:
            return problem
        self._effect("draft_email", {"to": to, "subject": subject, "body": body, "cc": cc or None})
        return (f"Draft opened in Mail to {', '.join(resolved)} — nothing has been "
                "sent. Tell the user to review it and hit send.")

    def t_send_email(self, to, subject, body, cc="", confirmed_self_send=False):
        if msg := self._own_address(to, "send_email", confirmed_self_send):
            return msg
        resolved, problem = self._emails_for(to)
        if problem:
            return problem
        bad = [r for r in self._split(cc) if not self._EMAIL_RE.match(r)]
        if bad:
            return (f"(not valid email addresses: {', '.join(bad)}. Don't guess an "
                    "address — look it up in the user's mail or ask them for it.)")
        self._effect("send_email", {"to": resolved, "subject": subject, "body": body, "cc": cc or None})
        return f"Email sent to {', '.join(resolved)}."

    def _email_by_id(self, message_id: str):
        return next((m for m in self.emails if m.message_id and m.message_id == (message_id or "").strip()), None)

    def prepare_email_reply(self, payload: dict) -> dict:
        """The Wisp app's `prepare_email_reply` bridge answer (action_tools.prepare_reply_args)."""
        m = self._email_by_id(payload.get("message_id", ""))
        if m is None:
            return {"ok": False, "error": "message not found"}
        return {"ok": True, "reply": {
            "message_id": m.message_id, "account": payload.get("account") or m.account,
            "account_id": payload.get("account_id") or f"acct-{m.account}",
            "from": H.USER_EMAILS[0], "to": [m.addr], "cc": [], "bcc": [],
            "subject": m.subject if m.subject.lower().startswith("re:") else f"Re: {m.subject}",
            "content": str(payload.get("body") or "")}}

    def t_reply_to_email(self, message_id, body, reply_all=False, expected_reply=None, account=""):
        from service.tasks.reply_contract import make_receipt, validate_envelope
        m = self._email_by_id(message_id)
        if m is None:
            return f"(reply NOT sent: no email with id {message_id})"
        envelope = validate_envelope(expected_reply)
        if envelope is None:
            return "(reply NOT sent: the reply was not prepared and approved in Mail)"
        self._effect("reply_to_email", {"message_id": m.message_id, "body": body})
        return make_receipt(envelope)

    def t_draft_message(self, to, text):
        from service.tools.registry import _literal_message_recipient, _prepared_message_draft
        if type(to) is not str or type(text) is not str:
            return "(error: Draft recipient and body must be strings.)"
        to = to.strip(" ")
        if not to or any(ord(c) < 32 or ord(c) == 127 for c in to):
            return "(no recipient — ask the user who to text)"
        if not text.strip():
            return "(nothing to draft — the message body is empty)"
        literal = _literal_message_recipient(to)
        if literal is None:
            handle, problem = self.resolve_recipient(to)
            if problem:
                return problem
            literal = _literal_message_recipient(handle)
        if literal is None:
            return "(error: Choose one supported literal phone number or email address.)"
        self._effect("draft_message", {"to": literal, "text": text})
        return _prepared_message_draft(literal, text)

    def t_send_message(self, to, text, confirmed_self_send=False):
        if msg := self._own_address(to, "send_message", confirmed_self_send):
            return msg
        shown = (to or "").strip()
        if not shown:
            return "(no recipient — ask the user who to text)"
        display = shown
        if not (self._EMAIL_RE.match(shown) or re.fullmatch(r"[+()\-.\s\d]{7,}", shown)):
            handle, problem = self.resolve_recipient(shown)
            if problem:
                return problem
            display = f"{shown} ({handle})"
        if not (text or "").strip():
            return "(nothing to send — the message body is empty)"
        self._effect("send_message", {"to": display, "text": text})
        return f"Message sent to {display}."

    def t_schedule_send(self, channel, to, when, body="", text="", subject=""):
        from service.tools.timeranges import BadWhen, resolve_when
        channel = (channel or "").strip().lower()
        if channel not in ("email", "message"):
            return "(channel must be 'email' or 'message')"
        recipients = self._split(to)
        if len(recipients) > 1:
            return ("(NOT scheduled — schedule_send supports exactly one recipient. "
                    "Schedule each recipient separately; nothing was queued.)")
        msg = body or text or ""
        if not msg.strip():
            return "(nothing to send — pass `body` with the full message)"
        try:
            when_dt, _lbl = resolve_when(when)
        except BadWhen as exc:
            return str(exc)
        if when_dt.replace(tzinfo=None) <= self.now:
            return (f"(NOT scheduled — {when!r} resolves to {when_dt:%a %b %-d at %-I:%M %p}, which is in "
                    "the past. Ask the user what time they actually meant, or send it now with "
                    f"{'send_email' if channel == 'email' else 'send_message'}.)")
        if not recipients:
            return ("(no recipient — ask the user who this should go to)" if channel == "email"
                    else "(no recipient — ask the user who to text)")
        display = recipient = recipients[0]
        if channel == "email" and not self._EMAIL_RE.match(recipient):
            recipient, problem = self.resolve_recipient(recipient, want_email=True)
            if problem:
                return problem
            display = f"{to} ({recipient})"
        elif channel == "message" and not (self._EMAIL_RE.match(recipient)
                                           or re.fullmatch(r"[+()\-.\s\d]{7,}", recipient)):
            recipient, problem = self.resolve_recipient(recipient)
            if problem:
                return problem
            display = f"{to} ({recipient})"
        sid = f"{len(self.effects) + 1:012x}"
        self._effect("schedule_send", {"channel": channel, "to": recipient, "when": when, "body": msg,
                                       "subject": subject or None})
        pretty = when_dt.strftime("%a %b %-d at %-I:%M %p")
        return (f"Scheduled: {'email' if channel == 'email' else 'text'} to {display} "
                f"on {pretty} (id {sid}). It sends automatically — Wisp needs to be "
                "running at that time. Use cancel_scheduled_send to call it off.")
