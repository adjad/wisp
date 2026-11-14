"""Ask's two Study tools: find a lecture slide, and make study material from slides.

study_slides is a local read: it searches the deck pages Study already read
(store.search_pages) and never contacts anything. Each result names its deck and
page outside the fence, so the answer (or a reminder Ask sets next) can say
"Lecture 7, page 4"; the slide's own words (title, points, figure description)
are the student's school data and sit inside an untrusted fence.

study_make sends the chosen pages to the Study model, the same provider and
consent as the Study tab's own Make button (generate.make, the Study connector),
saves the result in study.db and returns its title and id. Nothing else leaves
the Mac. It asks first (fs_write) and refuses before any write when Study is not
connected or no page matches, with a not_done receipt so the chat stays usable.

Neither tool ever creates study.db: with no school data on this Mac they say so.
"""
from __future__ import annotations

import asyncio
import re

from service.tools import untrusted
from service.tools.registry import not_done, register

KINDS = ("flashcards", "quiz", "guide")
_NOUN = {"flashcards": "flash cards", "quiz": "quiz", "guide": "study guide"}
SEARCH_LIMIT = 5             # pages one study_slides answer shows
MAKE_SEARCH_LIMIT = 8        # pages a query-only study_make builds from
MAX_PAGES = 40               # the contract's cap for POST /study/generate pages
MAKE_S = 100.0               # same bound as the Study tab's /study/generate
_POINTS = 6
_CAP_LINE = 300
_CAP_FIGURE = 600
_WS = re.compile(r"\s+")

NO_STUDY = ("No lecture slides yet: add slides in the Study tab and Wisp reads them as pages. "
            "Nothing found.")


def _line(value, cap: int = _CAP_LINE) -> str:
    """One neutralized line of school text, collapsed and capped."""
    text = _WS.sub(" ", str(value or "")).strip()
    return untrusted.neutralize(text[:cap])


def _store():
    """The Study store when this Mac has school data, else None (never creates it)."""
    from service.study.store import study_exists, study_store
    return study_store() if study_exists() else None


def _search(store, query: str, course_id: str | None, limit: int) -> list[dict]:
    from service.study import store as store_module
    search = getattr(store, "search_pages", None) or getattr(store_module, "search_pages", None)
    if search is None:
        return []
    return list(search(query, course_id=course_id, limit=limit) or [])


def _course(store, course: str) -> dict | None:
    """The course the user named ("CS 20", "biology"), by code or name; None if unclear."""
    want = _WS.sub(" ", str(course or "")).strip().casefold()
    if not want:
        return None
    rows = list(store.courses() or [])
    exact = [c for c in rows if want in {str(c.get("code") or "").casefold(),
                                         str(c.get("name") or "").casefold()}]
    if len(exact) == 1:
        return exact[0]
    loose = [c for c in rows if want in str(c.get("code") or "").casefold()
             or want in str(c.get("name") or "").casefold()]
    return loose[0] if len(loose) == 1 else None


def _where(hit: dict) -> str:
    """'Lecture 7: Pointers, page 4', the words an answer or reminder repeats."""
    deck = _line(hit.get("deck")) or "Slides"
    return f"{deck}, page {int(hit.get('page') or 0)}"


def format_hits(hits: list[dict], query: str) -> str:
    out = [f"{len(hits)} slide page(s) from the student's own decks for “{_line(query, 120)}”. "
           "Name the deck and page in the answer (or a reminder title). To make flash cards, "
           "a quiz or a guide from them, call study_make with these source_id/page pairs."]
    for n, hit in enumerate(hits, 1):
        title = _line(hit.get("title"))
        out.append(f"{n}. {_where(hit)}" + (f" — {title}" if title else "")
                   + f" [source_id {_line(hit.get('source_id'), 80)}, page {int(hit.get('page') or 0)}]")
        body = []
        points = [p for p in (hit.get("points") or []) if isinstance(p, str) and p.strip()]
        body += ["- " + _line(p) for p in points[:_POINTS]]
        if figure := _line(hit.get("figure"), _CAP_FIGURE):
            body.append("Figure: " + figure)
        topics = [_line(t, 60) for t in (hit.get("topics") or []) if isinstance(t, str) and t.strip()]
        if topics:
            body.append("Topics: " + ", ".join(topics[:5]))
        if body:
            out.append(untrusted.fence("\n".join(body), untrusted.SLIDE_TEXT))
    return "\n".join(out)


@register(
    "study_slides",
    "Search the student's lecture slides on this Mac. Returns deck, page, points and figure. Read-only.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "topic, term or lecture, e.g. pointers or Lecture 7"},
        "course": {"type": "string", "description": "optional course code or name"},
    }, "required": ["query"]},
    category="fs_read",
    aliases=["which slide covers pointers", "what lecture talks about recursion",
             "find the slide about mitosis", "what did lecture 7 say about", "in my lecture slides",
             "which lecture covers this", "show me the slides on", "my class slides about"],
)
async def study_slides(query: str, course: str = "") -> str:
    query = _WS.sub(" ", str(query or "")).strip()
    if not query:
        return "(error: say what to look for in the slides.)"
    store = await asyncio.to_thread(_store)
    if store is None:
        return NO_STUDY
    picked = await asyncio.to_thread(_course, store, course) if course else None
    hits = await asyncio.to_thread(_search, store, query, (picked or {}).get("id"), SEARCH_LIMIT)
    if not hits:
        return f"Nothing found in the student's slides for “{_line(query, 120)}”."
    return format_hits(hits[:SEARCH_LIMIT], query)


def _pages(raw) -> list[dict]:
    """Valid {"source_id", "page"} pairs, each once, at most MAX_PAGES."""
    out, seen = [], set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        sid, page = item.get("source_id"), item.get("page")
        if not isinstance(sid, str) or not sid.strip() or isinstance(page, bool) or not isinstance(page, int) or page < 1:
            continue
        key = (sid.strip(), page)
        if key not in seen:
            seen.add(key)
            out.append({"source_id": key[0], "page": page})
    return out[:MAX_PAGES]


def _pages_text(pages: list[dict], decks: dict[str, str]) -> str:
    """'Lecture 7 pages 4, 5 and 6' for a receipt."""
    groups: dict[str, list[int]] = {}
    for p in pages:
        groups.setdefault(p["source_id"], []).append(p["page"])
    parts = []
    for sid, nums in groups.items():
        nums = sorted(nums)
        listed = ", ".join(map(str, nums[:-1])) + f" and {nums[-1]}" if len(nums) > 1 else str(nums[0])
        parts.append(f"{decks.get(sid) or 'slides'} page{'s' if len(nums) > 1 else ''} {listed}")
    return "; ".join(parts)


@register(
    "study_make",
    "Make flash cards, a quiz or a study guide from lecture slides with the Study model; saves it.",
    {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "pages": {"type": "array", "items": {"type": "object", "properties": {
            "source_id": {"type": "string"}, "page": {"type": "integer"}}}},
        "query": {"type": "string", "description": "topic or lecture, when no pages are given"},
        "count": {"type": "integer", "description": "optional number of cards or questions"},
    }, "required": ["kind"]},
    category="fs_write",
    aliases=["make flash cards from my slides", "quiz me on lecture 7", "make a quiz from these slides",
             "study guide for week 5", "flashcards for the midterm", "make me a practice quiz"],
)
async def study_make(kind: str, pages: list | None = None, query: str = "", count: int | None = None) -> str:
    from service.study import connector, generate
    if kind not in KINDS:
        return not_done(f"(error: kind must be one of {', '.join(KINDS)}; nothing was made.)")
    query = _WS.sub(" ", str(query or "")).strip()
    store = await asyncio.to_thread(_store)
    if store is None:
        return not_done(NO_STUDY + " Nothing was made.")
    chosen = _pages(pages)
    decks: dict[str, str] = {}
    course_id = None
    if not chosen and query:
        hits = await asyncio.to_thread(_search, store, query, None, MAKE_SEARCH_LIMIT)
        chosen = _pages(hits)
        for hit in hits:
            decks.setdefault(str(hit.get("source_id")), _line(hit.get("deck")))
            course_id = course_id or hit.get("course_id")
    if not chosen:
        return not_done("Nothing found in the student's slides to make it from. Nothing was made. "
                        "Call study_slides first, or ask which lecture to use.")
    for sid in {p["source_id"] for p in chosen} - set(decks):
        row = await asyncio.to_thread(store.source, sid)
        if row is None:
            return not_done(f"(error: no deck {_line(sid, 80)} in Study; nothing was made.)")
        decks[sid] = _line(row.get("title"))
        course_id = course_id or row.get("course_id")
    state = await connector.status_async()
    if not state.get("ready"):
        return not_done(connector.MESSAGES["missing"] + " Set up Study in the Study tab. Nothing was made.")
    courses = {c.get("id"): c for c in (await asyncio.to_thread(store.courses) or [])}
    course = courses.get(course_id) if course_id else None
    try:
        made = await asyncio.wait_for(
            generate.make(kind, course=course, assignment=None, topic=query or None,
                          count=count, pages=chosen),
            MAKE_S)
    except TimeoutError:
        return not_done(connector.MESSAGES["unreachable"] + " Nothing was saved.")
    except connector.StudyConnectorError as exc:
        return not_done(f"{exc.message} Nothing was saved.")
    except ValueError:
        return not_done(connector.MESSAGES["bad_reply"] + " Nothing was saved.")
    saved = await asyncio.to_thread(store.save_material, {
        "kind": kind, "title": made["title"], "course_id": course_id, "assignment_id": None,
        "model": state.get("active_model_id"), "sample": False, "body": made["body"]})
    return (f"Made {_NOUN[kind]} “{_line(saved.get('title'), 120)}” (id {saved.get('id')}) from "
            f"{_pages_text(chosen, decks)}. It's saved in the Study tab.")
