"""Quizzes Wisp prepares before a test (docs/STUDY_TAB.md "Ready before class").

IMPORTANT rules:
- Only with the student's consent: meta "auto_prepare" is True (off by default).
- Only when the Study connector is ready and the course has slides (deck pages).
- At most MAX_PER_DAY quizzes in any 24 hours, and at most one per assignment, ever.
- ``kick`` starts a background thread and returns at once: a GET never waits for it,
  never fails because of it, and two preparations never run at the same time.
- Misses lean the quiz: pages the student missed in that course go first.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from service.study import match

log = logging.getLogger(__name__)

MAX_PER_DAY = 3
WINDOW_S = 48 * 3600
QUESTIONS = 10
MAX_PAGES = 40
KICK_EVERY_S = 10 * 60
GENERATE_S = 100.0
TESTS = ("quiz", "exam")
PREPARED = "auto_prepared"     # meta: {assignment_id: {"material_id", "at", "slides", "count"}}
CONSENT = "auto_prepare"

_running = threading.Lock()
_last_kick = 0.0


def consented(store) -> bool:
    try:
        return store.meta(CONSENT, False) is True
    except Exception:  # noqa: BLE001
        return False


def prepared(store) -> dict:
    try:
        value = store.meta(PREPARED, {})
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}


def _due(a: dict) -> float | None:
    value = a.get("due_ts")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def candidates(store, now: float) -> list[dict]:
    """Open quizzes and exams due within WINDOW_S that have no quiz yet (Wisp's or the student's)."""
    done = prepared(store)
    has_quiz = {m.get("assignment_id") for m in store.materials()
                if isinstance(m, dict) and m.get("kind") == "quiz" and m.get("assignment_id")}
    out = []
    for a in store.assignments():
        if not isinstance(a, dict) or a.get("kind") not in TESTS:
            continue
        due = _due(a)
        if due is None or not now < due <= now + WINDOW_S:
            continue
        if (a.get("status") or "upcoming") in ("submitted", "graded"):
            continue
        if a.get("id") in done or a.get("id") in has_quiz:
            continue
        out.append(a)
    return sorted(out, key=lambda a: (_due(a), str(a.get("id"))))


def missed_pages(store, course_id: str | None, now: float) -> list[dict]:
    """[{"source_id", "page"}] the student missed in this course in the last two weeks, most missed first."""
    if not hasattr(store, "misses"):
        return []
    try:
        rows = store.misses(course_id, now - 14 * 86400)
    except Exception:  # noqa: BLE001
        return []
    counts: dict[tuple[str, int], int] = {}
    for r in rows or ():
        if isinstance(r, dict) and isinstance(r.get("source_id"), str) and isinstance(r.get("page"), int):
            key = (r["source_id"], r["page"])
            counts[key] = counts.get(key, 0) + 1
    return [{"source_id": s, "page": p} for (s, p), _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def plan_pages(store, assignment: dict, matches: list[dict], now: float) -> list[dict]:
    """Missed pages of matched decks first, then the matched pages; at most MAX_PAGES."""
    matched = [{"source_id": m["source_id"], "page": p} for m in matches for p in m.get("pages") or ()]
    allowed = {(r["source_id"], r["page"]) for r in matched}
    first = [r for r in missed_pages(store, assignment.get("course_id"), now)
             if (r["source_id"], r["page"]) in allowed]
    out, seen = [], set()
    for r in first + matched:
        key = (r["source_id"], r["page"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out[:MAX_PAGES]


async def prepare(now: float | None = None, *, store=None, status=None, make=None) -> list[str]:
    """Make the quizzes that are due; returns the new material ids. Never raises for one
    assignment's failure: the next one is still tried (within the bounds)."""
    now = time.time() if now is None else now
    if store is None:
        from service.study.store import study_exists, study_store
        if not study_exists():
            return []
        store = study_store()
    if not consented(store):
        return []
    from service.study import connector, generate
    state = status if status is not None else await connector.status_async()
    if not state.get("ready"):
        return []
    make = make or generate.make
    courses = {c.get("id"): c for c in store.courses() if isinstance(c, dict)}
    made: list[str] = []
    for a in candidates(store, now):
        done = prepared(store)
        if sum(1 for v in done.values() if isinstance(v, dict) and (v.get("at") or 0) > now - 86400) >= MAX_PER_DAY:
            break
        matches = match.match(a, store)
        if not matches:
            continue
        refs = plan_pages(store, a, matches, now)
        course = courses.get(a.get("course_id"))
        pages = generate.resolve_pages(store, refs)
        if not pages:
            continue
        # Recorded before the model call: a failure is not retried on every GET.
        done[a["id"]] = {"material_id": None, "at": now, "slides": match.slides(matches), "count": QUESTIONS}
        store.set_meta(PREPARED, done)
        try:
            got = await asyncio.wait_for(
                make("quiz", course=course, assignment={**a, "course_name": (course or {}).get("name") or ""},
                     topic=None, count=QUESTIONS, context="", pages=pages), GENERATE_S)
        except Exception:  # noqa: BLE001 - one failure never stops the GET or the next assignment
            log.info("study: preparing a quiz failed", exc_info=False)
            continue
        saved = store.save_material({
            "kind": "quiz", "title": got["title"], "course_id": a.get("course_id"), "assignment_id": a["id"],
            "model": state.get("active_model_id"), "sample": False, "body": got["body"]})
        done = prepared(store)
        count = len((got.get("body") or {}).get("questions") or []) or QUESTIONS
        done[a["id"]] = {**done.get(a["id"], {}), "material_id": saved["id"], "count": count}
        store.set_meta(PREPARED, done)
        made.append(saved["id"])
    if made:
        try:
            from service.assistant.hub import hub
            await hub.publish({"type": "changed"})
        except Exception:  # noqa: BLE001
            pass
    return made


def _run(now: float, store_factory) -> None:
    try:
        store = store_factory() if store_factory else None
        asyncio.run(prepare(now, store=store))
    except Exception:  # noqa: BLE001
        log.debug("study prepare skipped", exc_info=True)
    finally:
        _running.release()


def kick(now: float | None = None, *, store=None, store_factory=None, force: bool = False) -> bool:
    """Start one background preparation when consent is on; True when a thread started.
    Cheap and non-blocking: a run already going, or one started in the last KICK_EVERY_S, is enough."""
    global _last_kick
    now = time.time() if now is None else now
    try:
        if store is not None and not consented(store):
            return False
        if not force and time.monotonic() - _last_kick < KICK_EVERY_S and _last_kick:
            return False
        if not _running.acquire(blocking=False):
            return False
        _last_kick = time.monotonic()
        factory = store_factory or ((lambda: store) if store is not None else None)
        try:
            threading.Thread(target=_run, args=(now, factory), name="study-prepare", daemon=True).start()
        except Exception:  # noqa: BLE001
            _running.release()
            return False
        return True
    except Exception:  # noqa: BLE001
        return False
