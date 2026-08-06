"""Notes.app search — raw, verbatim lookup (no summarization; there's nothing
to digest — a note IS the content the user wants back).

Read by the Swift app (NotesReader.swift, Notes Automation access) and pushed
to /assistant/sync/notes once/day, same reasoning as email_tools.py's raw
cache: note bodies are personal content, so this uses the same TTL-purge
lifecycle rather than sitting in memory indefinitely.
"""
from __future__ import annotations

import time
from datetime import datetime

from service.tools import cache_store
from service.tools.registry import register

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


async def search_notes_impl(query: str | None = None, count: int = 5) -> str:
    _purge_if_expired()
    if not _notes.strip():
        return ("(No note content cached right now — either Notes.app hasn't synced "
                "yet, or the cache expired after a day and is waiting on the next "
                "daily sync. Try again shortly.)")
    rows = _parse()
    rows.sort(key=lambda r: r["ts"], reverse=True)  # most recently modified first
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


def all_notes_text() -> str:
    """Every cached note (title/folder/body), verbatim — for the profile
    builder (service/memory/profile.py). No query filtering, just the raw
    cache; covers roughly the 100 most-recently-modified notes."""
    _purge_if_expired()
    blocks = []
    for r in sorted(_parse(), key=lambda r: r.get("ts") or 0):
        folder = f" [{r['folder']}]" if r["folder"] else ""
        # Modification date included so the profile builder can tell a note
        # written years ago from one edited this week — without it, stale notes
        # read as current facts. Oldest-first for the same reason.
        when = (datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
                if r.get("ts") else "unknown date")
        blocks.append(f"(note last edited {when}) {r['title']}{folder}\n{r['body']}")
    return "\n\n---\n\n".join(blocks)


@register(
    "search_notes",
    "Get the RAW, verbatim content of the user's Notes.app notes — not a "
    "summary. Use when the user asks about something they wrote down or saved "
    "in Notes: a list, an idea, a code/password they jotted, project notes, "
    "etc. Pass `query` to search titles/folders/body for a keyword; omit it "
    "for the most recently modified notes. Covers roughly the 100 most "
    "recent notes.",
    {"type": "object",
     "properties": {
         "query": {"type": "string", "description": "keyword to search for in title, folder, or body"},
         "count": {"type": "integer", "description": "max notes to return in full (default 5)"},
     }},
    category="notes_read",
)
async def search_notes(query: str | None = None, count: int = 5) -> str:
    return await search_notes_impl(query, count)
