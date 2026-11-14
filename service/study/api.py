"""Routes for the Study tab (docs/STUDY_TAB.md).

IMPORTANT rules:
- Every change is the app's own JSON request (`native_only`), never a web page's.
- Page text from the app is untrusted data: it only ever reaches canvas_parse and,
  inside a delimited data block, generate.extract_school_data. Saved sources (Drive
  files, school pages) reach the model only inside generate.make's data block.
- Errors are {"detail": code, "message": gentle sentence}; the key never appears.
"""
from __future__ import annotations

import asyncio
import functools
import re
import time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from service.memory_saver_api import native_only
from service.study import canvas_parse, connector, generate, ready
from service.study import sample
from service.study.store import study_exists, study_store

router = APIRouter(prefix="/study", tags=["study"])

RECENT_PAST_S = 14 * 86400
WEEK_S = 7 * 86400
CONTEXT_MAX = 6000
DONE = ("submitted", "graded")
PAGE_CHARS = 150_000        # what the app's page reader sends at most (StudyPageReader.maxCharacters)
MAX_PAGES = 12
# Cost bounds (the app's own request timeout is 120 s, so the server stops well before it):
MODEL_PAGES = 3             # pages per import the Study model may read; the rest stay unrecognised
IMPORT_POSTS = 6            # paid POSTs per import, across those pages and their retries
IMPORT_MODEL_S = 75.0       # all of one import's model work
GENERATE_S = 100.0          # one /study/generate (its POSTs are capped by generate.MAX_POSTS)
STOP_READING = ("missing", "rejected", "busy", "unreachable")   # no more pages go to the model
# Long context (connector.long_context: the active Study model is Gemini). Same 120 s app
# timeout; the model reads every school page it could not parse, 4 at a time.
LONG_MODEL_PAGES = MAX_PAGES
LONG_IMPORT_POSTS = 2 * MAX_PAGES
LONG_IMPORT_MODEL_S = 100.0
LONG_CONCURRENCY = 4
MAX_SOURCE_IDS = 20
MAX_SLIDE_REFS = 40
WHOLE_COURSE_CONTEXT = 100_000   # assignment descriptions of a whole course (long context)

_HTTP = {"missing": 409, "rejected": 502, "busy": 502, "unreachable": 502, "bad_reply": 502}
NOT_FOUND = "Wisp couldn't find that in Study. It may have been cleared."
BAD_BODY = "Wisp couldn't read that request."


class Refusal(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _answer(route):
    """Turn a Refusal or a connector failure into the contract's error shape."""
    @functools.wraps(route)
    async def wrapped(*args, **kwargs):
        try:
            return await route(*args, **kwargs)
        except Refusal as exc:
            return JSONResponse({"detail": exc.code, "message": exc.message}, status_code=exc.status)
        except connector.StudyConnectorError as exc:
            return JSONResponse({"detail": exc.code, "message": exc.message},
                                status_code=_HTTP.get(exc.code, 502))
    return wrapped


# --- bodies -----------------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(_Strict):
    url: StrictStr = Field(max_length=4000)
    title: StrictStr = Field(default="", max_length=1000)
    text: StrictStr = Field(max_length=PAGE_CHARS)


class ImportBody(_Strict):
    pages: list[Page] = Field(min_length=1, max_length=MAX_PAGES)
    timezone: StrictStr | None = Field(default=None, max_length=100)


class ConnectorBody(_Strict):
    provider: StrictStr = Field(max_length=40)
    model_id: StrictStr = Field(default="", max_length=200)


class SlideRef(_Strict):
    source_id: StrictStr = Field(max_length=100)
    page: StrictInt = Field(ge=1, le=5000)


class GenerateBody(_Strict):
    kind: Literal["guide", "quiz", "lesson", "flashcards"]
    course_id: StrictStr | None = Field(default=None, max_length=300)
    assignment_id: StrictStr | None = Field(default=None, max_length=300)
    topic: StrictStr | None = Field(default=None, max_length=300)
    count: StrictInt | None = Field(default=None, ge=1, le=30)
    source_ids: list[Annotated[StrictStr, Field(max_length=100)]] | None = Field(
        default=None, max_length=MAX_SOURCE_IDS)
    whole_course: StrictBool = False
    pages: list[SlideRef] | None = Field(default=None, max_length=MAX_SLIDE_REFS)


class AttemptBody(_Strict):
    correct: StrictInt = Field(ge=0, le=1000)
    total: StrictInt = Field(ge=1, le=1000)
    missed: list[Annotated[StrictInt, Field(ge=0, le=999)]] | None = Field(default=None, max_length=1000)


class SettingsBody(_Strict):
    auto_prepare: StrictBool


class SampleBody(_Strict):
    timezone: StrictStr | None = Field(default=None, max_length=100)


async def _body(request: Request, model):
    try:
        data = await request.json()
    except ValueError as exc:
        raise Refusal(422, "invalid", BAD_BODY) from exc
    if not isinstance(data, dict):
        raise Refusal(422, "invalid", BAD_BODY)
    try:
        return model(**data)
    except (ValidationError, TypeError) as exc:
        raise Refusal(422, "invalid", BAD_BODY) from exc


def _timezone(name: str | None) -> str:
    """The app's zone when it sends one, else this Mac's."""
    if not name:
        from service.behavior.timeutil import resolve_tz, tz_name
        return tz_name(resolve_tz())
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise Refusal(422, "invalid", "Wisp didn't recognise that time zone.") from exc
    return name


async def _changed() -> None:
    """Best effort: the Brief and Day tabs show what is due, so nudge them."""
    try:
        from service.assistant.hub import hub
        await hub.publish({"type": "changed"})
    except Exception:  # noqa: BLE001
        pass


# --- the state --------------------------------------------------------------------

def _visible(item: dict, now: float) -> bool:
    due = item.get("due_ts")
    status = item.get("status")
    if due is None:
        return status not in DONE
    if due >= now:
        return True
    return status in ("missing", "late") and due >= now - RECENT_PAST_S


def _empty_state(connector_state: dict) -> dict:
    return {"connector": connector_state, "sample": False, "last_import": None,
            "courses": [], "assignments": [], "materials": [], "sources": []}


def _existing_store():
    """The store when this Mac has school data, else None. Never creates study.db or the
    study folder: only routes that save something open it with study_store()."""
    return study_store() if study_exists() else None


def _shown_name(course: dict) -> str:
    """A course's name for the state. A course page that showed no name saves a nameless row;
    it shows as "Course <canvas id>" instead of a blank chip (state only, never stored)."""
    name = (course.get("name") or "").strip()
    if name:
        return name
    match = re.fullmatch(r"[a-z]+:(\d+)", course.get("id") or "")
    return f"Course {match.group(1)}" if match else ""


def state(now: float | None = None, *, connector_state: dict | None = None) -> dict:
    """The contract's state. Async routes pass connector_state from status_async()."""
    now = time.time() if now is None else now
    if connector_state is None:
        connector_state = connector.status()
    store = _existing_store()
    if store is None:
        return _empty_state(connector_state)
    courses = store.courses()
    names = {c["id"]: _shown_name(c) for c in courses}
    rows = store.assignments()   # already due_ts ascending, nulls last
    due_counts: dict[str, int] = {}
    for item in rows:
        due = item.get("due_ts")
        if item.get("status") not in DONE and due is not None and now <= due <= now + WEEK_S:
            due_counts[item.get("course_id")] = due_counts.get(item.get("course_id"), 0) + 1
    listed = []
    for c in courses:
        due_count = due_counts.get(c["id"], 0)
        graded = c.get("score") is not None or c.get("grade") is not None
        listed.append({"id": c["id"], "name": names[c["id"]], "code": c.get("code"),
                       "grade": c.get("grade"), "score": c.get("score"), "url": c.get("url"),
                       "due_count": due_count, "active": due_count > 0 or graded,
                       "sample": bool(c.get("sample"))})
    # Something due first, then courses with a grade, then the rest (orientation modules and
    # the like); alphabetical inside each group.
    listed.sort(key=lambda c: (0 if c["due_count"] else 1 if c["active"] else 2,
                               c["name"].casefold(), c["id"]))
    return {
        "connector": connector_state,
        "sample": store.has_sample(),
        "last_import": store.meta("last_import"),
        "courses": listed,
        "assignments": [{"id": a["id"], "course_id": a.get("course_id"),
                         "course_name": names.get(a.get("course_id"), ""), "title": a.get("title") or "",
                         "kind": a.get("kind") or "assignment", "due_ts": a.get("due_ts"),
                         "all_day": bool(a.get("all_day")), "points": a.get("points"), "score": a.get("score"),
                         "status": a.get("status") or "upcoming", "url": a.get("url"),
                         "sample": bool(a.get("sample"))}
                        for a in rows if _visible(a, now)],
        "materials": store.materials(),
        "sources": store.sources(),
    }


# --- routes -----------------------------------------------------------------------

async def _state() -> dict:
    return state(connector_state=await connector.status_async())


@router.get("/state")
@_answer
async def get_state():
    got = await _state()
    _kick()
    return got


def _kick() -> None:
    """Best effort: prepared quizzes (ready.py) start in the background, only with consent."""
    try:
        store = _existing_store()
        if store is not None:
            ready.kick(store=store)
    except Exception:  # noqa: BLE001
        pass


def _recognized(parsed) -> bool:
    return parsed.kind != "other" or bool(parsed.courses or parsed.assignments)


@router.post("/import", dependencies=[Depends(native_only)])
@_answer
async def import_pages(request: Request):
    body = await _body(request, ImportBody)
    zone = _timezone(body.timezone)
    now = time.time()
    pages = body.pages
    parsed = [canvas_parse.parse_page(p.url, p.title, p.text, now=now, timezone=zone) for p in pages]
    unread = [i for i, (p, got) in enumerate(zip(pages, parsed))
              if got.kind == "other" and canvas_parse.is_school_url(p.url)]
    # One status read either way: with no page for the model it is also the reply's connector.
    status = await connector.status_async()
    ready = bool(status.get("ready"))
    long_context = ready and bool(status.get("long_context"))
    if long_context:
        read = await _read_pages_together(pages, parsed, unread, now=now, zone=zone)
    else:
        read = set()
        await _read_pages_in_turn(pages, parsed, unread, ready, now=now, zone=zone)
    results, courses, assignments = [], [], []
    for page, got in zip(pages, parsed):
        results.append(_page_result(page, got, courses, assignments))
    counts = (0, 0)
    store = None
    if any(r["recognized"] for r in results):
        store = study_store()
        store.clear_sample()
        counts = store.import_rows(courses, assignments)   # one row per course and per assignment
        store.set_meta("last_import", now)
    if long_context:
        keep = [i for i, r in enumerate(results)
                if i in read or (r["recognized"] and canvas_parse.is_school_url(pages[i].url))]
        if keep:
            store = store or study_store()
            _save_page_sources(store, [(pages[i], parsed[i]) for i in keep])
    if store is not None:
        await _changed()
    final = await _state() if unread else state(connector_state=status)
    return {"courses": counts[0], "assignments": counts[1], "pages": results, "state": final}


async def _read_pages_in_turn(pages, parsed: list, unread: list[int], ready: bool, *, now: float,
                              zone: str) -> None:
    """The ordinary import: at most MODEL_PAGES pages, one after another, within
    IMPORT_POSTS and IMPORT_MODEL_S. Fills ``parsed`` in place."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + IMPORT_MODEL_S
    model_pages = 0
    with connector.post_budget(IMPORT_POSTS):
        for i in unread:
            page = pages[i]
            remaining = deadline - loop.time()
            if not (ready and model_pages < MODEL_PAGES and remaining > 0 and connector.posts_left() > 0):
                continue
            model_pages += 1
            try:
                parsed[i] = await asyncio.wait_for(
                    generate.extract_school_data(page.url, page.title, page.text, now=now, timezone=zone),
                    remaining)
            except TimeoutError:
                ready = False   # the import's model time is spent
            except connector.StudyConnectorError as exc:
                if exc.code in STOP_READING:
                    ready = False   # a refused key or a busy model fails the next page too
            except ValueError:
                pass   # this page stays unrecognised; the others still count


async def _read_pages_together(pages, parsed: list, unread: list[int], *, now: float, zone: str) -> set[int]:
    """The long-context import: up to LONG_MODEL_PAGES pages, LONG_CONCURRENCY at a time,
    within LONG_IMPORT_POSTS and LONG_IMPORT_MODEL_S. Fills ``parsed`` in place (so the
    results keep the request's order) and returns the indexes the model read."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LONG_IMPORT_MODEL_S
    gate = asyncio.Semaphore(LONG_CONCURRENCY)
    stop = False
    read: set[int] = set()

    async def one(i: int) -> None:
        nonlocal stop
        async with gate:
            remaining = deadline - loop.time()
            if stop or remaining <= 0 or connector.posts_left() <= 0:
                return
            page = pages[i]
            try:
                got = await asyncio.wait_for(
                    generate.extract_school_data(page.url, page.title, page.text, now=now, timezone=zone,
                                                 long_context=True),
                    remaining)
            except TimeoutError:
                stop = True   # the import's model time is spent
                return
            except connector.StudyConnectorError as exc:
                if exc.code in STOP_READING:
                    stop = True   # pages not yet started are not sent
                return
            except ValueError:
                return
            parsed[i] = got
            read.add(i)

    # Tasks copy this context, so they all share the one request-wide POST budget.
    with connector.post_budget(LONG_IMPORT_POSTS):
        await asyncio.gather(*(one(i) for i in unread[:LONG_MODEL_PAGES]))
    return read


def _save_page_sources(store, items: list) -> None:
    """Each kept school page becomes a "page" source, tied to its course when the page
    names exactly one course this import saved."""
    from service.study.store import merge_key
    stored = [c for c in store.courses() if not c.get("sample")]
    ids = {c["id"] for c in stored}
    by_key = {}
    for c in stored:
        by_key.setdefault(c.get("merge_key") or merge_key(c.get("name")), set()).add(c["id"])
    for page, got in items:
        found: set[str] = set()
        for c in got.courses:
            cid = c.get("id")
            if cid in ids:
                found.add(cid)
            else:
                match = by_key.get(merge_key(c.get("name")), set())
                if len(match) == 1:
                    found |= match
        for a in got.assignments:
            if a.get("course_id") in ids:
                found.add(a["course_id"])
        store.save_source({"kind": "page", "url": page.url, "title": page.title or page.url,
                           "text": page.text, "course_id": next(iter(found)) if len(found) == 1 else None})


def _page_result(page, parsed, courses: list, assignments: list) -> dict:
    recognized = _recognized(parsed)
    if recognized:
        courses.extend(parsed.courses)
        assignments.extend(parsed.assignments)
    return {"url": page.url, "kind": parsed.kind, "recognized": recognized}


@router.post("/connector", dependencies=[Depends(native_only)])
@_answer
async def set_connector(request: Request):
    body = await _body(request, ConnectorBody)
    try:
        return await asyncio.to_thread(connector.set_choice, body.provider, body.model_id)
    except ValueError as exc:
        raise Refusal(422, "invalid", "Wisp doesn't know that Study provider or model.") from exc


def _context(assignment: dict | None, course: dict | None, assignments: list[dict]) -> str:
    """What else the course covers. The assignment's own description is not repeated here:
    generate.make already puts it in the data block."""
    parts = []
    if course:
        siblings = [a.get("title") for a in assignments
                    if a.get("course_id") == course.get("id") and a.get("title")
                    and (assignment is None or a.get("id") != assignment.get("id"))]
        if siblings:
            parts.append("Other work in this course:\n" + "\n".join("- " + t for t in siblings[:40]))
    return "\n\n".join(parts)[:CONTEXT_MAX]


def _chosen_sources(store, source_ids: list[str] | None, course_id: str | None) -> list[dict]:
    """The sources one material is made from: the ids asked for (an unknown one is 404), then,
    for a whole course, every source of that course. Full rows, each id once."""
    wanted = list(dict.fromkeys(source_ids or []))
    if (wanted or course_id) and store is None:
        if wanted:
            raise Refusal(404, "not_found", NOT_FOUND)
        return []
    if course_id:
        # The course's newest sources fill up to MAX_SOURCE_IDS in all: a whole course stays bounded.
        extra = [s["id"] for s in store.sources(course_id) if s["id"] not in wanted]
        wanted += extra[:max(0, MAX_SOURCE_IDS - len(wanted))]
    out = []
    for sid in wanted:
        row = store.source(sid)
        if row is None:
            raise Refusal(404, "not_found", NOT_FOUND)
        out.append(row)
    return out


def _chosen_slides(store, refs) -> list[dict]:
    """Full slide rows for the refs asked for; an unknown deck or page is 404."""
    if not refs:
        return []
    wanted = list(dict.fromkeys((r.source_id, r.page) for r in refs))
    if store is None or not hasattr(store, "pages"):
        raise Refusal(404, "not_found", NOT_FOUND)
    rows = generate.resolve_pages(store, [{"source_id": s, "page": p} for s, p in wanted])
    if len(rows) != len(wanted):
        raise Refusal(404, "not_found", NOT_FOUND)
    return rows


def _course_context(course: dict | None, assignments: list[dict], long_context: bool) -> str:
    """A whole course: every assignment of it with its description."""
    cap = WHOLE_COURSE_CONTEXT if long_context else CONTEXT_MAX
    parts = []
    for a in assignments:
        if not course or a.get("course_id") != course.get("id") or not a.get("title"):
            continue
        details = (a.get("details") or "").strip()
        parts.append(f"- {a['title']}" + (f"\n{details}" if details else ""))
    text = "Work in this course:\n" + "\n".join(parts) if parts else ""
    return text[:cap]


@router.post("/generate", dependencies=[Depends(native_only)])
@_answer
async def make_material(request: Request):
    body = await _body(request, GenerateBody)
    existing = _existing_store()
    courses = {c["id"]: c for c in existing.courses()} if existing else {}
    rows = existing.assignments() if existing else []
    assignment = None
    if body.assignment_id:
        assignment = next((a for a in rows if a["id"] == body.assignment_id), None)
        if assignment is None:
            raise Refusal(404, "not_found", NOT_FOUND)
    course_id = body.course_id or (assignment or {}).get("course_id")
    course = courses.get(course_id) if course_id else None
    if body.course_id and course is None:
        raise Refusal(404, "not_found", NOT_FOUND)
    topic = (body.topic or "").strip() or None
    if body.whole_course and not body.course_id:
        raise Refusal(422, "invalid", "Pick a course to study the whole course.")
    sources = _chosen_sources(existing, body.source_ids, course_id if body.whole_course else None)
    slides = _chosen_slides(existing, body.pages)
    if course is None and assignment is None and topic is None and not sources and not slides:
        raise Refusal(422, "invalid", "Pick a course, an assignment or a topic to study.")
    if assignment is not None:
        assignment = assignment | {"course_name": (course or {}).get("name") or ""}
    extra = {}
    long_context = False
    if sources or body.whole_course:
        long_context = bool((await connector.status_async()).get("long_context"))
        extra = {"sources": sources, "long_context": long_context}
    if slides:
        extra["pages"] = slides
    context = (_course_context(course, rows, long_context) if body.whole_course
               else _context(assignment, course, rows))
    try:
        made = await asyncio.wait_for(
            generate.make(body.kind, course=course, assignment=assignment, topic=topic,
                          count=body.count, context=context, **extra),
            GENERATE_S)
    except TimeoutError as exc:
        raise connector.StudyConnectorError("unreachable") from exc
    except ValueError as exc:
        raise connector.StudyConnectorError("bad_reply") from exc
    # The model that answered: on the Intel cloud fallback that is the cloud profile's, not the choice.
    model = (await connector.status_async()).get("active_model_id")
    return study_store().save_material({
        "kind": body.kind, "title": made["title"], "course_id": course["id"] if course else None,
        "assignment_id": assignment["id"] if assignment else None, "model": model, "sample": False,
        "body": made["body"]})


@router.get("/materials/{material_id}")
@_answer
async def get_material(material_id: str):
    store = _existing_store()
    found = store.material(material_id) if store else None
    if found is None:
        raise Refusal(404, "not_found", NOT_FOUND)
    return found


@router.delete("/materials/{material_id}", dependencies=[Depends(native_only)])
@_answer
async def delete_material(material_id: str):
    store = _existing_store()
    if store is None or not store.delete_material(material_id):
        raise Refusal(404, "not_found", NOT_FOUND)
    return {"deleted": True}


@router.delete("/sources/{source_id}", dependencies=[Depends(native_only)])
@_answer
async def delete_source(source_id: str):
    store = _existing_store()
    if store is None or not store.delete_source(source_id):
        raise Refusal(404, "not_found", NOT_FOUND)
    return {"state": await _state()}


@router.post("/materials/{material_id}/attempt", dependencies=[Depends(native_only)])
@_answer
async def record_attempt(material_id: str, request: Request):
    body = await _body(request, AttemptBody)
    if body.correct > body.total:
        raise Refusal(422, "invalid", BAD_BODY)
    store = _existing_store()
    best = store.record_attempt(material_id, body.correct, body.total) if store else None
    if best is None:
        raise Refusal(404, "not_found", NOT_FOUND)
    if body.missed:
        _record_misses(store, material_id, body.missed)
    return {"best_score": best}


def _miss_rows(store, material: dict, missed: list[int]) -> list[dict]:
    """One row per missed question: its slide (when it names one) and a short topic."""
    body = material.get("body") or {}
    if material.get("kind") == "quiz":
        items = body.get("questions") or []
    elif material.get("kind") == "lesson":
        items = [s.get("check") or {} for s in body.get("steps") or [] if isinstance(s, dict)]
    elif material.get("kind") == "flashcards":
        items = body.get("cards") or []
    else:
        items = []
    pages: dict[str, dict] = {}
    rows = []
    for i in dict.fromkeys(missed):
        if not 0 <= i < len(items) or not isinstance(items[i], dict):
            continue
        slide = items[i].get("slide") if isinstance(items[i].get("slide"), dict) else None
        sid = slide.get("source_id") if slide else None
        page = slide.get("page") if slide else None
        topic = None
        if isinstance(sid, str) and isinstance(page, int):
            if sid not in pages:
                try:
                    pages[sid] = {r.get("page"): r for r in store.pages(sid) or () if isinstance(r, dict)}
                except Exception:  # noqa: BLE001
                    pages[sid] = {}
            hit = pages[sid].get(page) or {}
            topics = [t for t in hit.get("topics") or [] if isinstance(t, str) and t.strip()]
            topic = topics[0].strip() if topics else (hit.get("title") or None)
        else:
            sid = page = None
        rows.append({"question": i, "source_id": sid, "page": page,
                     "topic": (topic or material.get("title") or "this material")[:120]})
    return rows


def _record_misses(store, material_id: str, missed: list[int]) -> None:
    """Best effort: a miss that cannot be saved never fails the attempt."""
    try:
        material = store.material(material_id)
        rows = _miss_rows(store, material or {}, missed)
        if rows and hasattr(store, "record_misses"):
            store.record_misses(material_id, rows)
    except Exception:  # noqa: BLE001
        pass


@router.get("/settings")
@_answer
async def get_settings():
    store = _existing_store()
    return {"auto_prepare": ready.consented(store) if store is not None else False}


@router.post("/settings", dependencies=[Depends(native_only)])
@_answer
async def set_settings(request: Request):
    body = await _body(request, SettingsBody)
    store = study_store()
    store.set_meta(ready.CONSENT, bool(body.auto_prepare))
    if body.auto_prepare:
        try:
            ready.kick(store=store, force=True)
        except Exception:  # noqa: BLE001
            pass
    await _changed()
    return {"auto_prepare": bool(body.auto_prepare)}


@router.post("/sample", dependencies=[Depends(native_only)])
@_answer
async def load_sample(request: Request):
    body = await _body(request, SampleBody)
    sample.load(study_store(), now=time.time(), timezone=_timezone(body.timezone))
    await _changed()
    return await _state()


@router.delete("/data", dependencies=[Depends(native_only)])
@_answer
async def clear_data():
    store = _existing_store()   # nothing saved means nothing to clear, and nothing is created
    if store is not None:
        store.clear()
        await _changed()
    return await _state()
