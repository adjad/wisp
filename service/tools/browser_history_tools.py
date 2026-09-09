"""Safari + Chrome browsing history — read by the Swift app
(BrowserHistoryReader.swift, direct SQLite read of History.db / Chrome's
History under Wisp.app's Full Disk Access grant) and pushed to
/assistant/sync/browser_history, same split as Messages: chat.db-style
SQLite files are TCC-protected and this Python backend is a separate process
that can't share the app's grant.

Off by default (see the Settings toggle) and, unlike Mail/Notes, only ever
carries host + path + title — BrowserHistoryReader strips query strings
before this ever sees a row, since those routinely carry search text or
auth/reset tokens.

Two independent caches (safari/chrome) so one browser's absence — not
installed, or Full Disk Access not yet granted — never wipes the other's
data, same reasoning as imessage_tools' separate contacts/messages fields.
"""
from __future__ import annotations

import time

from service.tools import cache_store
from service.tools.registry import register
from service.tools.timeranges import PERIOD_ARG, BadPeriod, resolve_period, resolve_span

_BROWSERS = ("safari", "chrome")
_raw: dict[str, str] = {b: cache_store.load(f"browser_history_{b}") for b in _BROWSERS}
_synced_at: dict[str, float] = {b: (time.time() if _raw[b] else 0.0) for b in _BROWSERS}
_available: dict[str, bool] = {b: False for b in _BROWSERS}
_reason: dict[str, str] = {b: "waiting for first sync" for b in _BROWSERS}
_completed: set[str] = set()
_enabled: bool | None = None

# Syncs every 30 min (BrowserHistoryReader); a couple of missed cycles just
# means the toggle was off or the app wasn't running, not that the data is
# stale in a misleading way, so this is generous rather than tight like
# Notes' once-a-day TTL.
_TTL_SECONDS = 3 * 3600


def cache_browser_history(browser: str, raw: str, available: bool, reason: str = "") -> None:
    if browser not in _BROWSERS:
        return
    _available[browser] = available
    _reason[browser] = reason
    _completed.add(browser)
    if not available:
        return
    _raw[browser] = raw
    _synced_at[browser] = time.time()
    cache_store.save(f"browser_history_{browser}", raw)


def set_browser_history_enabled(enabled: bool) -> None:
    global _enabled
    if enabled and _enabled is False:
        _completed.clear()
        _available.update({browser: False for browser in _BROWSERS})
    _enabled = enabled


def browser_history_sync_state() -> str:
    if _enabled is False:
        return "disabled"
    if len(_completed) < len(_BROWSERS):
        return "syncing"
    return "ready" if any(_available.values()) else "unavailable"


def _purge_if_expired() -> None:
    now = time.time()
    for b in _BROWSERS:
        if _synced_at[b] and (now - _synced_at[b]) > _TTL_SECONDS:
            _raw[b] = ""
            _synced_at[b] = 0.0


def _parse(browser: str) -> list[dict]:
    out: list[dict] = []
    for line in _raw[browser].splitlines():
        parts = line.split(" | ", 3)
        if len(parts) != 4:
            continue
        ts_s, host, path, title = parts
        try:
            ts = float(ts_s)
        except ValueError:
            continue
        out.append({"ts": ts, "host": host.strip(), "path": path.strip(),
                     "title": title.strip(), "browser": browser})
    return out


def _all_rows() -> list[dict]:
    _purge_if_expired()
    if _enabled is False:
        return []
    rows: list[dict] = []
    for b in _BROWSERS:
        if _available[b]:
            rows.extend(_parse(b))
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def _matches(query: str, host: str, path: str, title: str) -> bool:
    q = query.lower()
    return q in host.lower() or q in path.lower() or q in title.lower()


async def search_browser_history_impl(query: str | None = None, count: int | None = None,
                                      period: str | None = None, day: str | None = None) -> str:
    from service.assistant.sync_status import ensure_sources
    await ensure_sources(("browser_history",))
    state = browser_history_sync_state()
    if state == "syncing":
        return "Wisp is still checking and syncing your browser history. Try again in a moment."
    if state == "disabled":
        return "Browser History is turned off in Wisp Settings."
    if state == "unavailable":
        return "Wisp could not read browser history. Check Browser History and Full Disk Access in Settings."
    rows = _all_rows()
    if not rows:
        return "No browsing history found in the completed sync of browsers Wisp could read."
    total = len(rows)
    # `period` (a range) wins over `day` (one day) when both are supplied —
    # same convention as summarize_emails/view_emails.
    label = None
    if period:
        try:
            start, end, label = resolve_span(period)
        except BadPeriod as e:
            return str(e)
        rows = [r for r in rows if start <= r["ts"] < end]
    elif day:
        try:
            start, end, label = resolve_period(day)
        except BadPeriod as e:
            return str(e)
        rows = [r for r in rows if start <= r["ts"] < end]
    if label is not None and not rows:
        # Zero rows in the scoped window doesn't mean the history is empty —
        # verified bug (2026-08-16): "browser history for today" was answered
        # from the default-10 most-recent slice, which can miss a whole day of
        # earlier browsing. Say the range was quiet instead of implying there's
        # nothing at all, so the model doesn't silently under-report.
        return (f"No browsing history in {label} — but there are {total} more "
                f"recent visits outside that range. Call this again with no "
                f"`period`/`day` to see them.")
    note = ""
    if query:
        matched = [r for r in rows if _matches(query, r["host"], r["path"], r["title"])]
        if matched:
            rows = matched
        else:
            note = f"(no match for {query!r} in recent history)\n\n"
            rows = []
    # Without a period/day, `count` caps the most-recent slice (default 10) —
    # with one, the range itself is the scope, so return everything in it
    # unless the caller explicitly asked for a smaller cap.
    if count is not None:
        rows = rows[:count]
    elif label is None:
        rows = rows[:10]
    if not rows:
        return note or "No matching history found."
    blocks = []
    for r in rows:
        when = time.strftime("%a %b %-d, %Y %-I:%M %p", time.localtime(r["ts"]))
        title = f" — {r['title']}" if r["title"] else ""
        blocks.append(f"[{r['browser']}] {when}  {r['host']}{r['path']}{title}")
    return note + "\n".join(blocks)


@register(
    "search_browser_history",
    "Look through the user's recent Safari/Chrome browsing history (site, "
    "page path, and page title only — never full URLs or search text). Use "
    "when the user asks about a site or page they recently visited or "
    "looked up. Pass `query` to search host/path/title for a keyword. Pass "
    "`period` for a date RANGE ('this month', 'last week') or `day` for one "
    "specific day ('today', 'yesterday', 'YYYY-MM-DD') when the user NAMED a "
    "time — 'what was I looking at today', 'sites from last week'. Omit both "
    "for a question with no time in it ('what have I been browsing?') to get "
    "the most recent visits. Only available if the user has turned on "
    "Browser History in Settings.",
    {"type": "object",
     "properties": {
         "query": {"type": "string", "description": "keyword to search for in site, path, or title"},
         "period": PERIOD_ARG,
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD' — scope to that whole day"},
         "count": {"type": "integer",
                   "description": "max visits to return (default 10 with no period/day; "
                                  "unlimited within the range when period/day is set)"},
     }},
    category="browser_history_read",
)
async def search_browser_history(query: str | None = None, count: int | None = None,
                                 period: str | None = None, day: str | None = None) -> str:
    return await search_browser_history_impl(query, count, period, day)
