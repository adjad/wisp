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

A full snapshot replaces both independent browser caches. An unavailable
browser clears only its own rows; disabling history clears both. Consent
revisions prevent an older request from undoing a later privacy change.
"""
from __future__ import annotations

import json
import time

from service.tools import cache_store
from service.tools.registry import register
from service.tools.timeranges import PERIOD_ARG, BadPeriod, resolve_period, resolve_span

_BROWSERS = ("safari", "chrome")
# A saved snapshot is not proof of consent in this process. Never restore
# sensitive rows until the app has rechecked access and supplied a fresh read.
_raw: dict[str, str] = {b: "" for b in _BROWSERS}
_synced_at: dict[str, float] = {b: (time.time() if _raw[b] else 0.0) for b in _BROWSERS}
_available: dict[str, bool] = {b: False for b in _BROWSERS}
_reason: dict[str, str] = {b: "waiting for first sync" for b in _BROWSERS}
_completed: set[str] = set()
try:
    _privacy_state = json.loads(cache_store.load("browser_history_privacy") or "{}")
    _revision = int(_privacy_state.get("revision", 0))
    _enabled: bool | None = False if _privacy_state.get("enabled") is False else None
except (ValueError, TypeError, AttributeError):
    _revision, _enabled = 0, None

# Highest observed revision fences older requests even when disk I/O fails.
# Completion is separate so the same failed request can safely retry.
_applied_revision = _revision
_privacy_current = False

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
    if _enabled is False:
        return
    if not available:
        _raw[browser] = ""
        _synced_at[browser] = 0.0
        _delete_history_files((browser,))
        return
    _raw[browser] = raw
    _synced_at[browser] = time.time()
    if raw:
        cache_store.save(f"browser_history_{browser}", raw)
    else:
        _delete_history_files((browser,))


def set_browser_history_enabled(enabled: bool) -> None:
    global _enabled
    if enabled and _enabled is False:
        _completed.clear()
        _available.update({browser: False for browser in _BROWSERS})
    _enabled = enabled
    if not enabled:
        # Invalidate memory before disk I/O: even a failed unlink must close
        # query access. The endpoint fails so the app retries the clear.
        _raw.update({browser: "" for browser in _BROWSERS})
        _synced_at.update({browser: 0.0 for browser in _BROWSERS})
        _available.update({browser: False for browser in _BROWSERS})
        _completed.clear()
        _delete_history_files(_BROWSERS)


def _delete_history_files(browsers) -> None:
    for browser in browsers:
        for suffix in ("txt", "tmp"):
            (cache_store.CACHE_DIR / f"browser_history_{browser}.{suffix}").unlink(missing_ok=True)


def apply_browser_history_sync(body: dict) -> bool:
    """Versioned full snapshot. Older/retried requests cannot undo a clear.

    Legacy per-browser pushes work only until a versioned client is seen.
    A new app sends both browsers together, including unavailable/empty ones.
    """
    global _revision, _applied_revision, _privacy_current
    revision = body.get("revision")
    if revision is not None and (type(revision) is not int or revision <= 0):
        raise ValueError("revision must be a positive integer")
    if ((revision is None and _revision)
            or (revision is not None and (revision < _revision or revision == _applied_revision))):
        return False
    enabled = body.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    snapshots = body.get("browsers") if revision is not None else {
        str(body.get("browser") or ""): body}
    if enabled:
        if not isinstance(snapshots, dict) or (revision is not None and set(snapshots) != set(_BROWSERS)):
            raise ValueError("enabled snapshot requires safari and chrome")
        for browser, snapshot in snapshots.items():
            if browser not in _BROWSERS or not isinstance(snapshot, dict):
                raise ValueError("invalid browser snapshot")
            diag = snapshot.get("diagnostics", {})
            if not isinstance(diag, dict) or type(diag.get("available")) is not bool:
                raise ValueError("available must be a boolean")
            if not isinstance(snapshot.get("lines", ""), str):
                raise ValueError("lines must be a string")
    if revision is not None:
        _revision = revision
    _privacy_current = False
    # Invalidate the entire old full snapshot BEFORE any per-browser disk
    # I/O. Legacy per-browser pushes replace only their own browser.
    affected = _BROWSERS if revision is not None or not enabled else tuple(snapshots)
    _raw.update({browser: "" for browser in affected})
    _synced_at.update({browser: 0.0 for browser in affected})
    _available.update({browser: False for browser in affected})
    _completed.difference_update(affected)
    try:
        set_browser_history_enabled(enabled)
        if enabled:
            for browser, snapshot in snapshots.items():
                diag = snapshot["diagnostics"]
                cache_browser_history(browser, snapshot.get("lines", ""), diag["available"],
                                      str(diag.get("reason") or ""))
        if revision is not None:
            state = json.dumps({"revision": revision, "enabled": enabled})
            cache_store.save("browser_history_privacy", state)
            if cache_store.load("browser_history_privacy") != state:
                raise OSError("Could not persist browser privacy state")
            _applied_revision = revision
        _privacy_current = True
    except OSError:
        # No partial snapshot is available when persistence/clearing fails.
        _raw.update({browser: "" for browser in _BROWSERS})
        _synced_at.update({browser: 0.0 for browser in _BROWSERS})
        _available.update({browser: False for browser in _BROWSERS})
        _completed.clear()
        raise
    return True


def browser_history_privacy_status() -> dict:
    return {"revision": _revision, "current": _privacy_current}


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
