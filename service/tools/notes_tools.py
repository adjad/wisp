"""Notes.app search — raw, verbatim lookup (no summarization; there's nothing
to digest — a note IS the content the user wants back).

Read by the Swift app (NotesReader.swift, Notes Automation access) and pushed
to /assistant/sync/notes once/day, same reasoning as email_tools.py's raw
cache: note bodies are personal content, so this uses the same TTL-purge
lifecycle rather than sitting in memory indefinitely.
"""
from __future__ import annotations

import re
import time

from service.tools import cache_store
from service.tools.registry import register
from service.tools.timeranges import PERIOD_ARG, BadPeriod, resolve_period, resolve_span

_FS = "\x01"
_RS = "\x02"
_notes: str = ""
_notes_at: float = 0.0
_available: bool | None = None
_reason = ""
# Notes sync only ONCE A DAY (NotesReader.swift), so before this cache was
# persisted a backend restart left it empty for up to 24h — long enough that
# `build_profile` routinely ran with no notes at all. Restoring on import (and
# treating restored content as fresh, hence _notes_at = now) closes that
# window; the TTL below still purges genuinely stale content as before.
_TTL_SECONDS = 24 * 3600


# Freshness bookkeeping for on-demand lookups (see sync_status.refresh_notes).
#   _snapshot_at          when the data we HOLD was received (restored caches:
#                         the cache file's mtime, so a restart cannot make a
#                         day-old snapshot look new)
#   _snapshot_started_at  when the native read that produced it began (0 = the
#                         app did not say)
#   _received_at / _receipt_*  the latest publish of any kind, including an
#                         "unavailable" one, and when ITS read began
# A publish only proves freshness if its native read STARTED after the lookup
# asked for one: a read already in flight when the request arrives finishes with
# an older view of Notes and must not satisfy it.
_snapshot_at: float = 0.0
_snapshot_started_at: float = 0.0
_received_at: float = 0.0
_receipt_started: float = 0.0
_receipt_available: bool | None = None
_receipt_reason = ""


def cache_notes(raw: str, available: bool = True, reason: str = "",
                snapshot_started_at: float | None = None) -> None:
    global _notes, _notes_at, _available, _reason, _snapshot_at
    global _snapshot_started_at, _received_at, _receipt_started
    global _receipt_available, _receipt_reason
    started = (float(snapshot_started_at)
               if isinstance(snapshot_started_at, (int, float))
               and not isinstance(snapshot_started_at, bool) else 0.0)
    if started and _receipt_started and started < _receipt_started:
        # A read that began earlier than one we already accepted finished late.
        # Taking it would roll the cache back to an older view of Notes.
        return
    now = time.time()
    _received_at, _receipt_started = now, started or _receipt_started
    _receipt_available, _receipt_reason = available, reason
    _available, _reason = available, reason
    if not available:
        return
    _notes = raw or ""
    _notes_at = _snapshot_at = now
    _snapshot_started_at = started
    cache_store.save("notes", _notes)


def notes_receipt() -> dict:
    """The latest publish from the app, for freshness decisions."""
    return {"received_at": _received_at, "started_at": _receipt_started,
            "available": _receipt_available, "reason": _receipt_reason}


def snapshot_age_seconds(now: float | None = None) -> float | None:
    """Age of the Notes data currently held, or None when there is none."""
    if not _notes.strip() and not _snapshot_at:
        return None
    when = _snapshot_started_at or _snapshot_at
    if not when:
        return None
    return max(0.0, (now if now is not None else time.time()) - when)


def notes_sync_state() -> str:
    if _available is None:
        return "syncing"
    return "ready" if _available else "unavailable"


_notes = cache_store.load("notes")
if _notes:
    _notes_at = time.time()
    try:
        import os
        _snapshot_at = os.path.getmtime(cache_store._path("notes"))
    except OSError:
        _snapshot_at = _notes_at


def _purge_if_expired() -> None:
    global _notes, _notes_at, _available
    if _notes_at and (time.time() - _notes_at) > _TTL_SECONDS:
        _notes = ""
        _notes_at = 0.0
        _available = None


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


# --- strict named-topic matching (opt-in) -----------------------------------
# A lookup like "what's the order for Amazon" names a TOPIC. Every significant
# term of it has to be present in the note (title, folder and body together, in
# any order and not necessarily adjacent): "Amazon" AND "order". The substring
# path above stays for callers that did not ask for this.
_STOP = frozenset(
    "a an the i me my we our you your is are am was were be been do does did have "
    "has had to of for in on at with and or but it this that what which who how "
    "when where can could would should please remember recall know about tell "
    "said say whats what's".split())
# Words that describe the request ("show my recent notes") rather than the topic.
_META = frozenset("note notes recent recently latest newest list show find search".split())


def significant_terms(query: str) -> list[str]:
    words = re.findall(r"\w+", (query or "").casefold())
    return list(dict.fromkeys(
        w for w in words if (len(w) > 1 or w.isdigit()) and w not in _STOP and w not in _META))


def is_browse_query(query: str | None) -> bool:
    """'recent notes' / 'my notes' name no topic: an explicitly vague browse."""
    if not query or not query.strip():
        return True
    words = re.findall(r"\w+", query.casefold())
    return not significant_terms(query) and any(w in _META for w in words)


def _stem(term: str) -> str:
    return term[:-1] if len(term) > 3 and term.endswith("s") else term


def all_terms_match(terms: list[str], *texts: str) -> bool:
    words = re.findall(r"\w+", " ".join(texts).casefold())
    return all(any(w.startswith(_stem(t)) for w in words) for t in terms)


def _human_age(seconds: float) -> str:
    if seconds < 90:
        return "under a minute"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)} minutes"
    hours = minutes / 60
    if hours < 36:
        n = round(hours)
        return f"{n} hour{'s' if n != 1 else ''}"
    n = round(hours / 24)
    return f"{n} days"


_NO_FALLTHROUGH = (" This is Notes' complete answer for this request: do not substitute "
                   "unrelated notes, and do not search other private sources (Mail, "
                   "Messages, Calendar) unless the user asks.")


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


def _freshness_prefix(receipt: dict | None) -> str:
    """Caveat for HITS taken from data that is not known to be current."""
    if receipt is None or receipt["status"] == "fresh":
        return ""
    age = receipt.get("age_seconds")
    how = f"a snapshot taken about {_human_age(age)} ago" if age is not None else "an older snapshot"
    why = receipt.get("reason") or "Notes could not be re-read just now"
    return (f"(Heads up: {why.rstrip('.')}. These results come from {how}, "
            "so a note created or edited since may be missing.)\n\n")


def _strict_miss(shown: str, receipt: dict | None, total: int, scope: str = "") -> str:
    """A truthful miss. Never claims absence the data cannot support."""
    age = snapshot_age_seconds()
    if receipt is not None and receipt["status"] == "fresh":
        return (f"No note matches {shown}{scope} — Notes was re-read just now (current) and "
                f"{total} notes were checked." + _NO_FALLTHROUGH)
    if receipt is None:
        when = f"about {_human_age(age)} ago" if age is not None else "at an unknown time"
        return (f"I found nothing matching {shown}{scope} in the last Notes snapshot (taken "
                f"{when}), but that snapshot was not re-read for this lookup, so I can't "
                "confirm no such note exists; one created since may be missing.")
    why = receipt.get("reason") or "Notes could not be re-read just now"
    when = f"about {_human_age(age)} old" if age is not None else "of unknown age"
    return (f"I found nothing matching {shown}{scope} in the last Notes snapshot ({when}), but "
            f"{why.rstrip('.')}, so I can't confirm no such note exists; one created "
            "since that snapshot may be missing. Say so plainly rather than claiming it "
            "does not exist, and offer to try again.")


async def search_notes_impl(query: str | None = None, count: int | None = None,
                            period: str | None = None, day: str | None = None,
                            strict: bool = False, refresh: bool = False) -> str:
    """`strict` and `refresh` are opt-in; callers that pass neither behave as before.

    strict   every significant term of `query` must match (AND), and a miss is a
             truthful "no matching note" with no unrelated recent-notes fallback
             (kept only for an explicit browse like "recent notes").
    refresh  ask the app for a NEW native read first and say honestly whether the
             data searched is current, stale (with its age), timed out or unavailable.
    """
    receipt: dict | None = None
    if refresh:
        from service.assistant.sync_status import refresh_notes
        receipt = await refresh_notes()
    _purge_if_expired()
    if not refresh:
        from service.assistant.sync_status import ensure_sources
        await ensure_sources(("notes",))
    have_data = bool(_notes.strip())
    if receipt is not None and receipt["status"] in {"unavailable", "timeout"} and not have_data:
        if receipt["status"] == "unavailable":
            return (f"Wisp could not read Notes right now. {receipt.get('reason', '')} I can't "
                    "tell whether a matching note exists.").strip()
        return ("Wisp could not get a read of your Notes in time, so I can't tell whether a "
                "matching note exists. Try again in a moment.")
    if receipt is None or not have_data:
        if notes_sync_state() == "syncing":
            return "Wisp is still syncing your notes. Try again once the sync finishes."
        if notes_sync_state() == "unavailable":
            return f"Wisp could not read Notes right now. {_reason}".strip()
    if not have_data and not strict:
        return "No notes found in the completed Notes sync."
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
        return (_freshness_prefix(receipt) +
                f"No notes modified in {label} — but there are {total} more notes "
                f"outside that range. Call this again with no `period`/`day` to "
                f"browse them.")
    topic = bool(query and query.strip()) and not (strict and is_browse_query(query))
    # An explicit count from the model always wins; otherwise browse wide and
    # search narrow (see _BROWSE_COUNT). A period/day range is itself the
    # scope, so return everything in it unless the caller capped it.
    if count is None:
        count = _SEARCH_COUNT if topic else (None if label is not None else _BROWSE_COUNT)
    note = _freshness_prefix(receipt)
    if topic and strict:
        terms = significant_terms(query)
        if terms:
            matched = [r for r in rows if all_terms_match(terms, r["title"], r["folder"], r["body"])]
            shown = " and ".join(repr(t) for t in terms)
            # a note whose title carries every term is the better answer
            matched.sort(key=lambda r: not all_terms_match(terms, r["title"]))
        else:
            matched = [r for r in rows if _matches(query, r["title"], r["folder"], r["body"])]
            shown = repr(query.strip())
        if not matched:
            return _strict_miss(shown, receipt, total, f" in {label}" if label else "")
        rows = matched
    elif topic:
        matched = [r for r in rows if _matches(query, r["title"], r["folder"], r["body"])]
        if matched:
            rows = matched
        else:
            count = max(count, 10)
            note += (f"(no exact match for {query!r} — showing your most recent notes "
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
    "notes. A search needs EVERY significant word of `query` to appear in the "
    "note (any order); a miss means no matching note — the result says whether "
    "Notes was re-read just now or only a snapshot of known age was searched, "
    "so report it truthfully and never swap in unrelated notes. Covers roughly "
    "the 100 most recent notes.",
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
    # The model-facing lookup is strict and asks for a fresh read whenever it was
    # given something to look for; an empty call is a plain browse of the cache.
    named = bool(query and query.strip())
    return await search_notes_impl(query, count, period, day, strict=named, refresh=named)
