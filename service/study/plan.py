"""What is due, and where study time fits (docs/STUDY_TAB.md "Brief and Day").

Pure: every input is passed in. Assignment rows are the store's rows; when the
caller knows them it adds ``course_name`` and ``course_code`` (day_hook does).
Times are epoch seconds; days are ISO dates in the given IANA timezone.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

from service.study import canvas_parse, match

DONE = {"submitted", "graded"}
TESTS = {"quiz", "exam"}
BLOCK_SECONDS = 45 * 60
MAX_BLOCKS = 2
UPCOMING_CAP = 8          # reference rows the Day and Brief tabs list
UPCOMING_DAYS = 7         # through the end of the local day this many days out
WINDOW = (8, 22)          # study blocks only between 08:00 and 22:00
LINE_LIMIT = 110
_UNKNOWN_END = 60 * 60    # a busy block with no end time is treated as an hour
REVIEW_MINUTES = 15
MAX_READY = 4
MAX_WEAK = 3
READY_DAYS = 7
WEAK_MIN = 2
_NUMBER_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
                 10: "ten"}


def _zone(timezone: str) -> ZoneInfo:
    return ZoneInfo(timezone)


def _due(a: dict) -> float | None:
    value = a.get("due_ts")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _open(a: dict) -> bool:
    return (a.get("status") or "upcoming") not in DONE and _due(a) is not None


def _local_day(ts: float, zone: ZoneInfo) -> date:
    return datetime.fromtimestamp(ts, zone).date()


def _bounds(day: date, zone: ZoneInfo) -> tuple[float, float]:
    return (datetime.combine(day, time(), zone).timestamp(),
            datetime.combine(day + timedelta(days=1), time(), zone).timestamp())


def _label(a: dict) -> str | None:
    code = (a.get("course_code") or a.get("code") or "").strip()
    if code:
        return code
    name = (a.get("course_name") or "").strip()
    return name if name and len(name) <= 18 else None


def _named(a: dict, *, brackets: bool) -> str:
    title = (a.get("title") or "").strip() or "Untitled"
    label = _label(a) if brackets else None
    if label and label.lower() not in title.lower():
        return f"{title} ({label})"
    return title


def _list(items: list[dict], *, brackets: bool, cap: int = 3, width: int | None = None) -> str:
    names = [_named(a, brackets=brackets) for a in items[:cap]]
    if width:
        names = [n if len(n) <= width else n[: width - 1].rstrip() + "…" for n in names]
    text = ", ".join(names)
    if len(items) > cap:
        text += f" and {len(items) - cap} more"
    return text


def _sorted(items):
    return sorted(items, key=lambda a: (_due(a), str(a.get("id") or "")))


def _day_word(ts: float, today: date, zone: ZoneInfo) -> str:
    d = _local_day(ts, zone)
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return d.strftime("%A")


# ---------------------------------------------------------------- one school item, once
#
# The store can still hold one assignment twice (two page kinds, a course split it could not merge,
# a copy with no course). What Brief and Day show collapses such twins by one key: the title
# (case and spacing folded, a trailing Canvas section code like " [STAT-101-02]" dropped) and the
# local due day; two timed copies must also share the due time, and two copies that each name a
# different course stay two items.

_SUFFIX = re.compile(r"\s*\[([^\[\]]{1,40})\]\s*$")
_SECTION = re.compile(r"[A-Z]{2,10}[ _-]?\d{1,4}[A-Z]?(?:[ ._-][A-Z0-9]{1,8}){0,3}")
_DONE_RANK = {"graded": 2, "submitted": 1}
_SAME_TIME = 60


def section_code(text: str) -> bool:
    """Whether a bracketed tail looks like a Canvas course-section code ("STAT-101-02")."""
    text = (text or "").strip()
    return bool(_SECTION.fullmatch(text)) and any(c.isdigit() for c in text)


def title_key(title) -> str:
    text = " ".join(str(title or "").split())
    found = _SUFFIX.search(text)
    if found and section_code(found.group(1)):
        text = text[: found.start()]
    return " ".join(text.split()).casefold()


def _course(a: dict, field: str) -> str:
    return " ".join(str(a.get(field) or "").split()).casefold()


def _same_item(a: dict, b: dict) -> bool:
    if not (a.get("all_day") or b.get("all_day")) and abs(_due(a) - _due(b)) > _SAME_TIME:
        return False
    if a.get("course_id") and a.get("course_id") == b.get("course_id"):
        return True
    for field in ("course_name", "course_code"):
        x, y = _course(a, field), _course(b, field)
        if x and y and x != y:
            return False
    return True


def _richness(a: dict) -> tuple:
    return (bool(_course(a, "course_name")), bool(_course(a, "course_code") or _course(a, "code")),
            bool(str(a.get("url") or "").strip()), bool(a.get("course_id")), not a.get("all_day"))


def collapse(assignments: list[dict], *, timezone: str) -> list[dict]:
    """The rows with each school item once (see above). The survivor is the row with a course
    name, code, address and due time, then the smallest id, so its id is stable across refreshes;
    it lists the others' ids as ``twins`` and takes the most done status (graded or submitted
    beats anything open, so handed-in work never comes back through its twin)."""
    zone = _zone(timezone)
    groups: list[list[dict]] = []
    by_key: dict[tuple, list[list[dict]]] = {}
    for a in assignments:
        if not isinstance(a, dict):
            continue
        key = title_key(a.get("title"))
        if _due(a) is None or not key:
            groups.append([a])
            continue
        slot = by_key.setdefault((key, _local_day(_due(a), zone)), [])
        group = next((g for g in slot if all(_same_item(a, b) for b in g)), None)
        if group is None:
            group = [a]
            slot.append(group)
            groups.append(group)
        else:
            group.append(a)
    out = []
    for group in groups:
        if len(group) == 1:
            out.append(group[0])
            continue
        ranked = sorted(group, key=lambda r: str(r.get("id") or ""))
        best = max(ranked, key=_richness)   # max keeps the smallest id on a tie
        merged = dict(best)
        for field in ("course_name", "course_code", "url"):
            if not str(merged.get(field) or "").strip():
                merged[field] = next((r.get(field) for r in ranked if str(r.get(field) or "").strip()),
                                     merged.get(field))
        done = max(ranked, key=lambda r: _DONE_RANK.get(r.get("status") or "", 0))
        if done.get("status") in DONE:
            merged["status"] = done["status"]
        merged["twins"] = [r.get("id") for r in ranked if r is not best]
        out.append(merged)
    return out


def _ids(a: dict) -> list:
    return [a.get("id"), *(a.get("twins") or [])]


def _found(table: dict | None, a: dict):
    if not table:
        return None
    return next((table[i] for i in _ids(a) if i in table), None)


def _line(today_items, tomorrow_items, near_test, *, now: float, zone: ZoneInfo, today: date) -> str:
    def build(brackets: bool, width: int | None) -> str:
        if today_items:
            head = f"{len(today_items)} due today: " + _list(today_items, brackets=brackets, width=width)
        elif tomorrow_items:
            head = ("Nothing due today. " f"{len(tomorrow_items)} due tomorrow: "
                    + _list(tomorrow_items, brackets=brackets, width=width))
        else:
            head = ""
        listed = {a.get("id") for a in (today_items or tomorrow_items)[:3]}
        if near_test and near_test.get("id") not in listed:
            word = "Exam" if near_test.get("kind") == "exam" else "Quiz"
            tail = f"{word} {_day_word(_due(near_test), today, zone)}: {(near_test.get('title') or '').strip()}"
            combined = f"{head}. {tail}" if head else tail
            if len(combined) <= LINE_LIMIT or not head:
                return combined
        return head

    for brackets, width in ((True, None), (False, None), (False, 28), (False, 18)):
        text = build(brackets, width)
        if len(text) <= LINE_LIMIT:
            return text
    return text[: LINE_LIMIT - 1].rstrip() + "…"


# ---------------------------------------------------------------- the link the student may click
#
# Brief and Day may show an "Open" button for an assignment's stored address. The student clicks it,
# so unlike the Find walker's never-open rule (find.important), a quiz's or exam's own landing page is
# allowed (marked "quiz_page"). Anything that starts, takes, previews, submits or launches something,
# signs in or out, carries credentials or a one-time key, or is not a plain https school page is
# refused. The app re-checks the same short list before it opens anything (StudyLinks.swift).

LINK_WORDS = frozenset({"take", "attempt", "attempts", "preview", "start", "history", "speedgrader",
                        "externaltools", "lti", "launch", "login", "logout", "signin", "signout", "sso",
                        "saml", "oauth"})
_QUIZ_SEGMENTS = frozenset({"quizzes", "quiz", "exams", "exam", "assessments"})
_TOKEN = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    text = text.lower()
    return set(_TOKEN.findall(text)) | {re.sub(r"[^a-z0-9]", "", text)}


def link(url) -> tuple[str | None, str | None]:
    """(url, url_kind) for an assignment's stored address, or (None, None) when it must not be
    offered. url_kind is "quiz_page" for a quiz or exam landing page, else "assignment"."""
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2048:
        return None, None
    if any(not 0x21 <= ord(c) <= 0x7E or c in "\\#" for c in url):
        return None, None          # printable ASCII only: no spaces, controls, backslash or fragment
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None, None
    host = parts.hostname or ""
    if (parts.scheme != "https" or not host or parts.username is not None or parts.password is not None
            or "@" in parts.netloc or "%" in parts.netloc or port is not None or parts.fragment or not host.isascii()
            or any(label.startswith("xn--") for label in host.split("."))
            or not canvas_parse.is_school_url(url)):
        return None, None
    path, query = unquote(parts.path), unquote(parts.query)
    if "%" in path or "%" in query:
        return None, None          # encoded twice: read as refused, never guessed
    segments = [s.lower() for s in path.split("/") if s]
    if any(_words(s) & LINK_WORDS for s in segments):
        return None, None
    if any(a == "submissions" and b == "new" for a, b in zip(segments, segments[1:])):
        return None, None
    if query:
        from service.study.find import _SECRET_KEY   # the walker's signed / one-time key names
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if any(_words(k) & LINK_WORDS or _words(v) & LINK_WORDS or _SECRET_KEY.search(k.lower())
               for k, v in pairs):
            return None, None
        if _words(query) & LINK_WORDS:
            return None, None
    return url, ("quiz_page" if _QUIZ_SEGMENTS & set(segments) else "assignment")


def _item(a: dict) -> dict:
    url, kind = link(a.get("url"))
    return {"id": a.get("id"), "title": a.get("title"), "course_name": a.get("course_name"),
            "due_ts": _due(a), "all_day": bool(a.get("all_day")), "kind": a.get("kind") or "assignment",
            "status": a.get("status") or "upcoming", "url": url, "url_kind": kind}


DUE_SOON_CAP = 4
_MATERIAL_ORDER = ("guide", "lesson", "flashcards", "quiz")


def _newest(materials: list[dict]) -> list[dict]:
    return sorted(materials, key=lambda m: (-(m.get("created_at") or 0) if isinstance(m.get("created_at"), (int, float))
                                            else 0, str(m.get("id") or "")))


def _material_for(a: dict, materials: list[dict]) -> dict | None:
    """The newest material made for this assignment (guide, then lesson, cards, quiz), else the
    newest guide for its course, else None."""
    ids = {i for i in _ids(a) if i}
    mine = [m for m in materials if m.get("assignment_id") in ids and m.get("kind") in _MATERIAL_ORDER]
    for kind in _MATERIAL_ORDER:
        hit = next((m for m in mine if m.get("kind") == kind), None)
        if hit:
            return {"id": hit.get("id"), "kind": kind, "scope": "assignment", "title": hit.get("title") or ""}
    course = a.get("course_id")
    if course:
        hit = next((m for m in materials if m.get("kind") == "guide" and m.get("course_id") == course), None)
        if hit:
            return {"id": hit.get("id"), "kind": "guide", "scope": "course", "title": hit.get("title") or ""}
    return None


def _due_soon(items: list[tuple[dict, str]], materials) -> dict:
    rows = [m for m in (materials or ()) if isinstance(m, dict) and m.get("id")]
    rows = _newest(rows)
    out = [{**_item(a), "course_label": _label(a) or "", "day": word, "material": _material_for(a, rows)}
           for a, word in items[:DUE_SOON_CAP]]
    return {"due_soon": out, "due_soon_more": max(0, len(items) - DUE_SOON_CAP)}


def day_summary(assignments: list[dict], *, now: float, timezone: str, materials: list[dict] | None = None) -> dict:
    """``materials``: the store's materials (store.materials(), newest first) so ``due_soon`` can
    name the guide made for each item; no model call and no write."""
    zone = _zone(timezone)
    today = _local_day(now, zone)
    tomorrow = today + timedelta(days=1)
    week_end = now + 7 * 86400
    rows = collapse(assignments, timezone=timezone)
    open_items = _sorted([a for a in rows if _open(a)])
    due_today = [a for a in open_items if _local_day(_due(a), zone) == today]
    due_tomorrow = [a for a in open_items if _local_day(_due(a), zone) == tomorrow]
    due_week = [a for a in open_items if now <= _due(a) <= week_end]
    # Local today 00:00 through the end of local day today+7 (DST-safe: whole local days, not 7 * 86400).
    lo, hi = _bounds(today, zone)[0], _bounds(today + timedelta(days=UPCOMING_DAYS), zone)[1]
    window = [a for a in open_items if lo <= _due(a) < hi]
    tests = [a for a in due_week if a.get("kind") in TESTS]
    next_test = tests[0] if tests else None
    near = next(iter(a for a in tests if _local_day(_due(a), zone) <= today + timedelta(days=3)), None)
    return {
        "due_today": [_item(a) for a in due_today],
        "upcoming": [{**_item(a), "course_label": _label(a) or ""} for a in window[:UPCOMING_CAP]],
        "upcoming_more": max(0, len(window) - UPCOMING_CAP),
        "due_tomorrow_count": len(due_tomorrow),
        "due_week_count": len(due_week),
        "missing_count": sum(1 for a in rows if a.get("status") == "missing"),
        "next_test": ({"id": next_test.get("id"), "title": next_test.get("title"),
                       "course_name": next_test.get("course_name"), "due_ts": _due(next_test)}
                      if next_test else None),
        "line": _line(due_today, due_tomorrow, near, now=now, zone=zone, today=today),
        "sample": bool(rows) and all(bool(a.get("sample")) for a in rows),
        # Open work due today, then tomorrow, by due time: the Brief's Ready rows link to it.
        **_due_soon([(a, "today") for a in due_today] + [(a, "tomorrow") for a in due_tomorrow], materials),
    }


def _slide_fields(item: dict, found, *, review: bool) -> dict:
    """``found`` is (slides, covers_text, review_pages) for this assignment, or None."""
    slides, covers, pages = found if found else ([], None, [])
    item.update(slides=slides, covers_text=covers)
    if review and slides and pages:
        item["review"] = {"minutes": REVIEW_MINUTES, "pages": pages}
    return item


def deadlines(assignments: list[dict], *, day: str, timezone: str, slides: dict | None = None) -> list[dict]:
    zone = _zone(timezone)
    lo, hi = _bounds(date.fromisoformat(day), zone)
    out = []
    for a in _sorted([a for a in collapse(assignments, timezone=timezone) if _open(a)]):
        due = _due(a)
        if not lo <= due < hi:
            continue
        label = (a.get("course_code") or a.get("code") or a.get("course_name") or "").strip()
        title = (a.get("title") or "").strip() or "Untitled"
        item = {"id": f"study:{a.get('id')}", "title": f"{title} · {label}" if label else title,
                "due_ts": due, "source": "study"}
        if a.get("all_day"):
            item.update(all_day=True, due_date=day)
        if slides is not None:
            _slide_fields(item, _found(slides, a), review=True)
        out.append(item)
    return out


def _busy(busy) -> list[tuple[float, float]]:
    out = []
    for pair in busy or ():
        try:
            start, end = pair
        except (TypeError, ValueError):
            continue
        if not isinstance(start, (int, float)) or not math.isfinite(start):
            continue
        if not isinstance(end, (int, float)) or not math.isfinite(end) or end <= start:
            end = start + _UNKNOWN_END
        out.append((float(start), float(end)))
    return sorted(out)


def study_blocks(assignments: list[dict], busy: list[tuple[float, float]], *, now: float, timezone: str,
                 day: str, slides: dict | None = None, booked=None) -> list[dict]:
    """At most MAX_BLOCKS blocks, one per school item, in free time. ``booked``: the block ids
    already on the calendar; a block whose twin's id was booked keeps that id, so the booking is
    still recognised (blocks_api.mark)."""
    zone = _zone(timezone)
    today = _local_day(now, zone)
    if date.fromisoformat(day) != today:
        return []
    window_start = datetime.combine(today, time(WINDOW[0]), zone).timestamp()
    window_end = datetime.combine(today, time(WINDOW[1]), zone).timestamp()
    rounded = math.ceil(now / 1800) * 1800  # the next :00 or :30
    earliest = max(rounded, window_start)
    taken = _busy(busy)
    blocks: list[dict] = []
    candidates = _sorted([a for a in collapse(assignments, timezone=timezone) if _open(a)
                          and now < _due(a) <= now + 48 * 3600])
    booked = set(booked or ())
    for a in candidates:
        if len(blocks) >= MAX_BLOCKS:
            break
        limit = min(_due(a), window_end)
        cursor = earliest
        for start, end in sorted(taken):
            if end <= cursor:
                continue
            if cursor + BLOCK_SECONDS <= start:
                break
            cursor = max(cursor, end)
        if cursor + BLOCK_SECONDS > limit:
            continue
        ids = [f"study-block:{i}" for i in _ids(a)]
        block = {"id": next((i for i in ids if i in booked), ids[0]), "title": f"Study: {(a.get('title') or '').strip()}",
                 "start": cursor, "end": cursor + BLOCK_SECONDS, "kind": "study", "task_id": None,
                 "warnings": []}
        if slides is not None:
            _slide_fields(block, _found(slides, a), review=False)
        blocks.append(block)
        taken.append((block["start"], block["end"]))
    return sorted(blocks, key=lambda b: (b["start"], b["id"]))


# ---------------------------------------------------------------- slides: ready and weak

def ready_items(assignments: list[dict], found: dict, materials: dict, *, now: float, timezone: str) -> list[dict]:
    """Upcoming work the student's slides cover, soonest first (at most MAX_READY).

    ``found``: {assignment_id: (matches, from_phrase, covers_text)}; ``materials``:
    {assignment_id: {"material_id", "count", "prepared": bool, "from": str | None}} for a quiz
    that is already made. Text: "CS 20 quiz Thursday. I made a 10-question quiz from weeks 5
    and 6." / "PA 3 is due Friday. Lectures 7 and 8 cover it."."""
    zone = _zone(timezone)
    today = _local_day(now, zone)
    end = now + READY_DAYS * 86400
    out = []
    for a in _sorted([a for a in collapse(assignments, timezone=timezone) if _open(a)]):
        if len(out) >= MAX_READY:
            break
        due = _due(a)
        hit = _found(found, a)
        if not (now <= due <= end) or not hit or not hit[0]:
            continue
        matches, phrase, covers = hit
        kind = a.get("kind") if a.get("kind") in TESTS else "assignment"
        label = _label(a)
        title = (a.get("title") or "").strip() or "Untitled"
        when = _day_word(due, today, zone)
        if kind in TESTS:
            head = f"{label} {kind} {when}." if label else f"{title} is {when}."
        else:
            head = f"{title} is due {when}."
        made = _found(materials, a) or {}
        if made.get("material_id") and made.get("prepared"):
            source = made.get("from") or phrase
            tail = (f"I made a {made.get('count') or 10}-question quiz from {source}." if source
                    else f"I made a {made.get('count') or 10}-question quiz.")
        elif made.get("material_id"):
            tail = "Your practice quiz is ready."
        else:
            tail = covers or ""
        out.append({"id": f"ready:{a.get('id')}", "kind": kind, "assignment_id": a.get("id"),
                    "course": label or (a.get("course_name") or ""), "title": title, "due_ts": due,
                    "text": f"{head} {tail}".strip(), "material_id": made.get("material_id") or None,
                    "slides": match.slides(matches)})
    return out


def weak_items(misses: list[dict], *, course_labels: dict, material_courses: dict, decks: dict,
               now: float, since_days: int = 14) -> list[dict]:
    """Topics missed WEAK_MIN or more times in the last two weeks (at most MAX_WEAK), most first:
    "You missed 3 on pointers; here are those two slides again."."""
    groups: dict[tuple, dict] = {}
    for m in misses or ():
        if not isinstance(m, dict):
            continue
        at = m.get("at")
        if isinstance(at, (int, float)) and at < now - since_days * 86400:
            continue
        topic = (m.get("topic") or "").strip() if isinstance(m.get("topic"), str) else ""
        if not topic:
            continue
        course_id = m.get("course_id") or material_courses.get(m.get("material_id"))
        g = groups.setdefault((course_id, topic.casefold()), {"course_id": course_id, "topic": topic,
                                                             "count": 0, "pages": {}})
        g["count"] += 1
        sid, page = m.get("source_id"), m.get("page")
        if isinstance(sid, str) and isinstance(page, int) and not isinstance(page, bool):
            g["pages"].setdefault(sid, set()).add(page)
    out = []
    for g in sorted(groups.values(), key=lambda g: (-g["count"], g["topic"].casefold())):
        if g["count"] < WEAK_MIN or len(out) >= MAX_WEAK:
            continue
        slides = [{"source_id": sid, "deck": decks.get(sid) or "", "pages": sorted(pages)}
                  for sid, pages in sorted(g["pages"].items())]
        n = sum(len(s["pages"]) for s in slides)
        if n == 1:
            tail = "; here is that slide again."
        elif n:
            tail = f"; here are those {_NUMBER_WORDS.get(n, str(n))} slides again."
        else:
            tail = ". A quick review would help."
        cid = g["course_id"]
        out.append({"id": f"weak:{cid or 'none'}:{g['topic'].casefold()}", "course": course_labels.get(cid) or "",
                    "topic": g["topic"], "count": g["count"],
                    "text": f"You missed {g['count']} on {g['topic']}{tail}", "slides": slides})
    return out
