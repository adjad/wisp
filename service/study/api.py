"""Routes for the Study tab (docs/STUDY_TAB.md).

IMPORTANT rules:
- Every change is the app's own JSON request (`native_only`), never a web page's.
- Page text from the app is untrusted data: it only ever reaches canvas_parse and,
  inside a delimited data block, generate.extract_school_data.
- Errors are {"detail": code, "message": gentle sentence}; the key never appears.
"""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from service.memory_saver_api import native_only
from service.study import canvas_parse, connector, generate
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


class GenerateBody(_Strict):
    kind: Literal["guide", "quiz", "lesson", "flashcards"]
    course_id: StrictStr | None = Field(default=None, max_length=300)
    assignment_id: StrictStr | None = Field(default=None, max_length=300)
    topic: StrictStr | None = Field(default=None, max_length=300)
    count: StrictInt | None = Field(default=None, ge=1, le=30)


class AttemptBody(_Strict):
    correct: StrictInt = Field(ge=0, le=1000)
    total: StrictInt = Field(ge=1, le=1000)


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
            "courses": [], "assignments": [], "materials": []}


def _existing_store():
    """The store when this Mac has school data, else None. Never creates study.db or the
    study folder: only routes that save something open it with study_store()."""
    return study_store() if study_exists() else None


def state(now: float | None = None, *, connector_state: dict | None = None) -> dict:
    """The contract's state. Async routes pass connector_state from status_async()."""
    now = time.time() if now is None else now
    if connector_state is None:
        connector_state = connector.status()
    store = _existing_store()
    if store is None:
        return _empty_state(connector_state)
    courses = store.courses()
    names = {c["id"]: c.get("name") or "" for c in courses}
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
        listed.append({"id": c["id"], "name": c.get("name") or "", "code": c.get("code"),
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
    }


# --- routes -----------------------------------------------------------------------

async def _state() -> dict:
    return state(connector_state=await connector.status_async())


@router.get("/state")
@_answer
async def get_state():
    return await _state()


def _recognized(parsed) -> bool:
    return parsed.kind != "other" or bool(parsed.courses or parsed.assignments)


@router.post("/import", dependencies=[Depends(native_only)])
@_answer
async def import_pages(request: Request):
    body = await _body(request, ImportBody)
    zone = _timezone(body.timezone)
    now = time.time()
    ready = None
    model_pages = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + IMPORT_MODEL_S
    results, courses, assignments = [], [], []
    with connector.post_budget(IMPORT_POSTS):
        for page in body.pages:
            parsed = canvas_parse.parse_page(page.url, page.title, page.text, now=now, timezone=zone)
            if parsed.kind == "other" and canvas_parse.is_school_url(page.url):
                if ready is None:
                    ready = bool((await connector.status_async()).get("ready"))
                remaining = deadline - loop.time()
                if (ready and model_pages < MODEL_PAGES and remaining > 0
                        and connector.posts_left() > 0):
                    model_pages += 1
                    try:
                        parsed = await asyncio.wait_for(
                            generate.extract_school_data(page.url, page.title, page.text,
                                                         now=now, timezone=zone),
                            remaining)
                    except TimeoutError:
                        ready = False   # the import's model time is spent
                    except connector.StudyConnectorError as exc:
                        if exc.code in STOP_READING:
                            ready = False   # a refused key or a busy model fails the next page too
                    except ValueError:
                        pass   # this page stays unrecognised; the others still count
            results.append(_page_result(page, parsed, courses, assignments))
    counts = (0, 0)
    if any(r["recognized"] for r in results):
        store = study_store()
        store.clear_sample()
        counts = store.import_rows(courses, assignments)   # one row per course and per assignment
        store.set_meta("last_import", now)
        await _changed()
    return {"courses": counts[0], "assignments": counts[1], "pages": results, "state": await _state()}


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
    if course is None and assignment is None and topic is None:
        raise Refusal(422, "invalid", "Pick a course, an assignment or a topic to study.")
    if assignment is not None:
        assignment = assignment | {"course_name": (course or {}).get("name") or ""}
    try:
        made = await asyncio.wait_for(
            generate.make(body.kind, course=course, assignment=assignment, topic=topic,
                          count=body.count, context=_context(assignment, course, rows)),
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
    return {"best_score": best}


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
