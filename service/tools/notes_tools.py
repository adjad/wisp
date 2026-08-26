"""Notes.app search — raw, verbatim lookup (no summarization; there's nothing
to digest — a note IS the content the user wants back).

Read by the Swift app (NotesReader.swift, Notes Automation access) and pushed
to /assistant/sync/notes once/day, same reasoning as email_tools.py's raw
cache: note bodies are personal content, so this uses the same TTL-purge
lifecycle rather than sitting in memory indefinitely.
"""
from __future__ import annotations

import time

from service.tools import cache_store
from service.tools.registry import register
from service.tools.timeranges import PERIOD_ARG, BadPeriod, resolve_period, resolve_span

_FS = "\x01"
_RS = "\x02"
_notes: str = ""
_notes_at: float = 0.0
# Notes sync only ONCE A DAY (NotesReader.swift), so before this cache was
# persisted a backend restart left it empty for up to 24h — long enough that
# `build_profile` routinely ran with no notes at all. Restoring on import (and
# treating restored content as fresh, hence _notes_at = now) closes that
# window; the TTL below still purges genuinely stale content as before.
_TTL_SECONDS = 24 * 3600


def cache_notes(raw: str) -> None:
    global _notes, _notes_at
    _notes = raw or ""
    _notes_at = time.time()
    cache_store.save("notes", _notes)


_notes = cache_store.load("notes")
if _notes:
    _notes_at = time.time()


def _purge_if_expired() -> None:
    global _notes, _notes_at
    if _notes_at and (time.time() - _notes_at) > _TTL_SECONDS:
        _notes = ""
        _notes_at = 0.0


def _parse() -> list[dict]:
    out: list[dict] = []
    for record in _notes.split(_RS):
        record = record.strip()
        if not record:
            continue
        parts = record.split(_FS)
        if len(parts) != 4:
            continue
        ts_s, title, folder, body = parts
        try:
            ts = float(ts_s)
        except ValueError:
            continue
        out.append({"ts": ts, "title": title.strip(), "folder": folder.strip(),
                     "body": body.strip()})
    return out


def _matches(query: str, title: str, folder: str, body: str) -> bool:
    q = query.lower()
    return q in title.lower() or q in folder.lower() or q in body.lower()


# How many notes a BROWSE ("what's in my notes?") returns when the model names
# no count, versus a targeted search ("search my notes for latte").
#
# These were one number, 5, and 5 is the right size for a search — the caller
# already narrowed it — but far too thin for a browse. Measured 2026-08-09 on
# "What's in my notes?": the model called search_notes({}) and got exactly 5
# notes back, so the answer listed 5 and closed with "would you like more
# details on any specific note?" while 100 notes sat in the cache. Whether the
# user saw 5 or 12 came down to whether the model happened to reason its way to
# passing a count — the same answer, differing threefold on a coin flip.
#
# Deliberately fixed HERE rather than by telling the model to pass a bigger
# count: date arithmetic aside, this is the same principle as tools/timeranges
# ("Wisp resolves it, not the model"). A good default costs one extra prompt
# block of tokens on a browse (~0.1s by the project's own prompt-token rule)
# and removes a coin flip from the answer.
_BROWSE_COUNT = 15
_SEARCH_COUNT = 5


async def search_notes_impl(query: str | None = None, count: int | None = None,
                            period: str | None = None, day: str | None = None) -> str:
    _purge_if_expired()
    if not _notes.strip():
        return ("(No note content cached right now — either Notes.app hasn't synced "
                "yet, or the cache expired after a day and is waiting on the next "
                "daily sync. Try again shortly.)")
    rows = _parse()
    rows.sort(key=lambda r: r["ts"], reverse=True)  # most recently modified first
    total = len(rows)
    # `period` (a range) wins over `day` (one day) — same convention as
    # summarize_emails/view_emails/search_browser_history.
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
        return (f"No notes modified in {label} — but there are {total} more notes "
                f"outside that range. Call this again with no `period`/`day` to "
                f"browse them.")
    # An explicit count from the model always wins; otherwise browse wide and
    # search narrow (see _BROWSE_COUNT). A period/day range is itself the
    # scope, so return everything in it unless the caller capped it.
    if count is None:
        count = _SEARCH_COUNT if query else (None if label is not None else _BROWSE_COUNT)
    note = ""
    if query:
        matched = [r for r in rows if _matches(query, r["title"], r["folder"], r["body"])]
        if matched:
            rows = matched
        else:
            count = max(count, 10)
            note = (f"(no exact match for {query!r} — showing your most recent notes "
                    "below so you can look through them yourself)\n\n")
    if not rows:
        return "No notes found."
    rows = rows[:count]
    blocks = []
    for r in rows:
        when = time.strftime("%a %b %-d, %Y %-I:%M %p", time.localtime(r["ts"]))
        folder = f" [{r['folder']}]" if r["folder"] else ""
        blocks.append(f"Title: {r['title']}{folder}\nModified: {when}\n\n{r['body']}")
    return note + "\n\n---\n\n".join(blocks)


@register(
    "search_notes",
    "Get the RAW, verbatim content of the user's Notes.app notes — not a "
    "summary. Use when the user asks about something they wrote down or saved "
    "in Notes: a list, an idea, a code/password they jotted, project notes, "
    "etc. Pass `query` to search titles/folders/body for a keyword. Pass "
    "`period` for a date RANGE ('this month', 'last week') or `day` for one "
    "specific day ('today', 'yesterday', 'YYYY-MM-DD') when the user NAMED a "
    "time — 'notes from last week', 'what did I write down today'. Omit both "
    "for a question with no time in it to get the most recently modified "
    "notes. Covers roughly the 100 most recent notes.",
    {"type": "object",
     "properties": {
         "query": {"type": "string", "description": "keyword to search for in title, folder, or body"},
         "period": PERIOD_ARG,
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD' — scope to notes modified that day"},
         "count": {"type": "integer",
                   "description": "max notes to return in full — omit it and Wisp picks a sensible number"},
     }},
    category="notes_read",
)
async def search_notes(query: str | None = None, count: int | None = None,
                       period: str | None = None, day: str | None = None) -> str:
    return await search_notes_impl(query, count, period, day)
