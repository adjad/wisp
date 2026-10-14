"""Composite briefings with explicit scope and source-evidence limitations.

The daily tool includes each selected source once, preserving exclusions and
date/account/title constraints. Cached Reminders receipts, dated items and
undated metadata are read together. Missing evidence is reported as unknown;
current location and cached weather are unavailable in the existing backend.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import time

from service.tools.registry import register

_PER_SOURCE_TIMEOUT = 25.0
_DAILY_SOURCE_MAX_AGE = 20 * 60


async def _safe(label: str, coro) -> tuple[str, str | None]:
    """Run one source. Returns (text, error) — never raises.

    A briefing that dies because one of four sources timed out is worse than a
    briefing that says which one is missing.
    """
    try:
        return await asyncio.wait_for(coro, timeout=_PER_SOURCE_TIMEOUT), None
    except asyncio.TimeoutError:
        return "", f"{label} took too long to load"
    except Exception as e:  # noqa: BLE001 — one bad source must not kill the brief
        return "", f"{label} unavailable ({e})"


def _greeting(now: dt.datetime) -> str:
    h = now.hour
    if h < 12:
        return "Good morning"
    if h < 18:
        return "Good afternoon"
    return "Good evening"


def _native_receipt_current(receipt: dict, now: float) -> bool:
    stamp = receipt.get("snapshot_started_at")
    diagnostics = receipt.get("diagnostics") or {}
    return (receipt.get("available") is True and not receipt.get("syncing")
        and type(stamp) in (float, int) and math.isfinite(stamp)
        and 0 <= now - stamp <= _DAILY_SOURCE_MAX_AGE
        and receipt.get("complete", True) is True
        and diagnostics.get("complete", True) is True and not diagnostics.get("partial"))


async def _daily_calendar(args: dict, now: float, start: float, end: float) -> str:
    from service.tools import assistant_tools as tools
    with tools.assistant_store.transaction(write=False) as db:
        row = db.execute("SELECT payload FROM today_source_sync WHERE source='calendar'").fetchone()
        receipt = json.loads(row["payload"]) if row else {}
    coverage = receipt.get("diagnostics") or {}
    lo, hi = coverage.get("coverage_start"), coverage.get("coverage_end")
    covered = (type(lo) in (int, float) and type(hi) in (int, float)
               and math.isfinite(lo) and math.isfinite(hi) and lo <= start and hi >= end)
    if not _native_receipt_current(receipt, now) or not covered:
        return "Calendar unknown: missing, stale, partial, failed or insufficient date coverage; no cached absence is verified."
    result = await tools.get_upcoming(**args)
    if args.get("account"):
        result = (f"Calendar account filter: {args['account']}; account inventory coverage is unverified. "
                  "No cached matches do not establish account-wide absence.\n" + result)
    return result


async def _daily_activity(source: str, args: dict, now: float) -> str:
    if source == "Email":
        from service.tools import email_tools as mail
        age = now - mail._headers_at
        if mail.email_sync_state() != "ready" or not 0 <= age <= _DAILY_SOURCE_MAX_AGE:
            return "Email unknown: missing, stale, failed or syncing current header evidence."
        return await mail.summarize_emails(**args)
    from service.tools import imessage_tools as messages
    if messages.messages_sync_state() != "ready":
        return "Messages unknown: missing, failed or syncing current-launch read."
    return ("Messages freshness/coverage unknown: cache age and exhaustive coverage are unverified; excerpts or no cached matches do not prove current absence.\n"
            + await messages.summarize_messages(**args))


async def _daily_reminders(*, now: float, start: float, end: float, account: str,
                           query: str, include_overdue: bool, include_undated: bool) -> str:
    """Read dated rows, undated view and native receipt in one store transaction."""
    from service.tools import assistant_tools as tools
    store = tools.assistant_store
    with store.transaction(write=False) as db:
        receipt_row = db.execute("SELECT payload FROM today_source_sync WHERE source='reminders'").fetchone()
        receipt = json.loads(receipt_row["payload"]) if receipt_row else {}
        rows = tools.reminders_matching("all", query, now=now)
        view, undated = store.undated_listing(query) if include_undated and not account else ({}, [])
    native_current = _native_receipt_current(receipt, now)
    account_key = account.casefold()
    selected = []
    for row in rows:
        if row.get("status", "active") != "active":
            continue
        if account_key and str(row.get("account") or "").casefold() != account_key:
            continue
        when = row.get("when_ts")
        if type(when) not in (int, float) or not math.isfinite(when):
            continue
        if not (start <= when < end or include_overdue and when < now):
            continue
        selected.append(row)
    rows, hidden = tools._drop_stale_wisp_only(selected, now)
    matching, wisp = [], []
    for row in rows:
        sources = tools._schedule_sources(row)
        if "reminders" in sources:
            if native_current:
                matching.append(row)
        elif "manual" in sources and "calendar" not in sources:
            wisp.append(row)
    window = f"{dt.datetime.fromtimestamp(start).isoformat(timespec='minutes')} to {dt.datetime.fromtimestamp(end).isoformat(timespec='minutes')} (end excluded)"
    out = [f"Reminder window: {window}; overdue outside window {'included' if include_overdue else 'excluded'}."]
    if account:
        out.append(f"Exact reminder account label: {account}. Account inventory coverage is unverified.")
    if query:
        out.append(f"Reminder title filter: {query!r}.")
    if not native_current:
        out.append("Apple Reminders unknown: missing, stale, partial, failed or syncing source evidence; no native items are listed or verified.")
    else:
        out.append("Apple Reminders: current cached incomplete-item read; completed items excluded.")
        if not matching:
            out.append("No dated matches in the inspected cached rows; this does not establish absence outside the requested scope.")
    def lines(items):
        result = []
        for row in sorted(items, key=lambda value: (value["when_ts"], value["title"]))[:100]:
            when = row["when_ts"]
            state = "overdue" if when < now else "upcoming"
            result.append(f"- {state}: {row['title']} — {dt.datetime.fromtimestamp(when):%Y-%m-%d %H:%M}"
                          + (f" [{row['account']}]" if row.get("account") else ""))
        if len(items) > 100:
            result.append(f"{len(items) - 100} additional matching rows omitted; listing is partial.")
        return result
    out.extend(lines(matching))
    if wisp:
        out.append("Wisp-only records kept for review; Apple copy/status unverified:")
        out.extend(lines(wisp))
    if hidden:
        out.append(f"{hidden} older Wisp-only records withheld; Apple status unverified.")
    if include_undated:
        if account:
            out.append("Undated reminders unknown for the requested account: this cache lacks account identity on undated rows.")
        elif not native_current or view.get("freshness") != "current":
            out.append("Undated reminders unknown: no current matching native snapshot.")
        else:
            out.append("Undated incomplete reminders (no due date; neither overdue nor upcoming):")
            out.extend(f"- {row['title']}" for row in undated[:100])
            if view.get("status") != "complete" or len(undated) > 100:
                out.append(f"Partial undated listing: {min(100, len(undated))} shown; {view.get('stored', 0)} stored of {view.get('total', 0)} total.")
            elif not undated:
                out.append("No matches in the current undated snapshot.")
    else:
        out.append("Undated reminders excluded; no due date can place them in this time window.")
    return "\n".join(out)


@register(
    "daily_brief",
    "Grounded daily briefing: today's calendar, overdue and next-7-day reminders, "
    "undated reminders separately, and today's mail/messages. Explicit source "
    "exclusions and exact date/account/title filters take precedence over defaults. "
    "Weather requires an explicit location; current-location/weather cache is "
    "unavailable. Missing, stale or partial evidence is disclosed, never guessed.",
    {
        "type": "object",
        "properties": {
            "location": {"type": "string",
                         "description": "Explicit city for weather, e.g. 'Palo Alto,CA'. Never guess a location."},
            "days": {"type": "integer",
                     "description": "How far ahead to look on the calendar. Default 1 (today)."},
            "include_activity": {"type": "boolean",
                                 "description": "Include recent mail/messages activity. Default true."},
            "period": {"type": "string", "description": "Exact requested date/range for every included source; overrides default horizons."},
            "account": {"type": "string", "description": "Account scope for calendar/email and exact reminder account label. Messages account scope unsupported."},
            "query": {"type": "string", "description": "Calendar/reminder title filter; filtered email/message summaries unsupported, never silently broadened."},
            "reminder_days": {"type": "integer", "minimum": 1, "maximum": 60, "description": "Default upcoming reminder horizon; 7 days, independent of today's calendar."},
            "include_overdue": {"type": "boolean", "description": "Include overdue reminders outside the window. Default true only when period is omitted."},
            "include_undated": {"type": "boolean", "description": "Show undated reminders separately. Default true only when period is omitted."},
            **{f"include_{source}": {"type": "boolean", "description": f"Include {source}; default true. False excludes it before reading."}
               for source in ("calendar", "reminders", "email", "messages", "weather")},
        },
    },
    category="assistant_read",
    # NOT "good morning". A bare greeting is claimed by TRIVIAL_RE and answered
    # by the fast model with no tools, which is the RIGHT behavior — someone
    # saying hello wants a hello, and forcing a tool call on genuinely
    # conversational input is the failure `expect_tool_first` exists to avoid.
    # Listing it here would only have made the retrieval eval pass on a phrasing
    # that never reaches retrieval.
    aliases=["how's my day looking", "give me the rundown",
             "what's my day like today", "brief me", "walk me through today",
             "what do I need to know this morning", "give me the full picture for today",
             "run me through today"],
)
async def daily_brief(location: str = "", days: int = 1,
                      include_activity: bool = True, *, period: str = "", account: str = "",
                      query: str = "", reminder_days: int = 7,
                      include_overdue: bool | None = None, include_undated: bool | None = None,
                      include_calendar: bool = True, include_reminders: bool = True,
                      include_email: bool = True, include_messages: bool = True,
                      include_weather: bool = True) -> str:
    if (type(days) is not int or not 1 <= days <= 14 or type(reminder_days) is not int
            or not 1 <= reminder_days <= 60 or not all(isinstance(value, str)
            for value in (location, period, account, query))
            or not all(type(value) is bool for value in (include_activity, include_calendar,
                include_reminders, include_email, include_messages, include_weather))
            or any(value is not None and type(value) is not bool for value in (include_overdue, include_undated))):
        return "(error: daily briefing needs valid string filters, calendar days 1–14 and reminder_days 1–60; no sources were read.)"
    stamp = time.time()
    now = dt.datetime.fromtimestamp(stamp)
    period, account, query = period.strip(), account.strip(), query.strip()
    start, end = stamp, (now + dt.timedelta(days=reminder_days)).timestamp()
    if period:
        from service.tools.timeranges import resolve_span, BadPeriod
        try:
            start, end, _ = resolve_span(period, now=now)
        except BadPeriod as exc:
            return f"(error: daily briefing date/range unsupported: {exc}; no sources were read.)"
        if end - start > 62 * 86400:
            return "(error: daily briefing range exceeds 62 days; no sources were read.)"
    include_overdue = not period if include_overdue is None else include_overdue
    include_undated = not period if include_undated is None else include_undated
    jobs: list[tuple[str, object]] = []
    notices = []
    if include_calendar:
        if period and end <= stamp:
            notices.append("Calendar unknown for a past range: daily calendar reads support ongoing/future events; use a past-events read.")
        else:
            calendar_args = {"calendar_only": True, **({"period": period} if period else
                             {"period": "today"} if days == 1 else {"days": days})}
            if account:
                calendar_args["account"] = account
            if query:
                calendar_args["query"] = query
            if period:
                calendar_start, calendar_end = start, end
            elif days == 1:
                calendar_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                calendar_end = (now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()
            else:
                calendar_start, calendar_end = stamp, (now + dt.timedelta(days=days)).timestamp()
            jobs.append(("Schedule", _daily_calendar(calendar_args, stamp, calendar_start, calendar_end)))
    if include_reminders:
        jobs.append(("Reminders", _daily_reminders(now=stamp, start=start, end=end, account=account,
                    query=query, include_overdue=include_overdue, include_undated=include_undated)))
    if include_weather and location.strip():
        from service.tools.web_tools import get_weather
        jobs.append(("Weather", get_weather(location.strip(), period=period)))
    elif include_weather:
        notices.append("Weather unknown: no explicit location or verified cached current-location/weather source is available; ask for a city, never guess one.")
    if include_activity:
        date_args = {"period": period} if period else {"day": "today"}
        if include_email:
            if query:
                notices.append("Email unknown for the requested title filter: daily email summaries cannot represent it; use a filtered email read.")
            else:
                jobs.append(("Email", _daily_activity("Email", {**date_args, **({"account": account} if account else {})}, stamp)))
        if include_messages:
            if account or query:
                notices.append("Messages unknown for the requested account/title filter: this summary API cannot represent it; no unfiltered read was substituted.")
            else:
                jobs.append(("Messages", _daily_activity("Messages", date_args, stamp)))

    # Local reads and an optional weather request share one bounded gather.
    # Messages retains its existing summarizer; this module creates no model
    # client and makes no additional model calls of its own.
    results = await asyncio.gather(*(_safe(label, coro) for label, coro in jobs))

    out = [f"{_greeting(now)} — {now.strftime('%A, %B %-d')}."]
    problems: list[str] = []

    for (label, _), (text, err) in zip(jobs, results):
        if err:
            problems.append(f"{err}; source outcome unknown")
            continue
        body = (text or "").strip()
        if not body:
            problems.append(f"{label} returned no usable evidence; source outcome unknown")
            continue
        out.append(f"\n{label}:\n{body}")

    out.extend("\n" + notice for notice in notices)
    if problems:
        out.append("\nCouldn't include: " + "; ".join(problems) + ".")
    return "\n".join(out)


