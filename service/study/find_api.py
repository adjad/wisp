"""Routes for Find materials (docs/STUDY_TAB.md, "Find materials"): classify the links the app
found on a course's Canvas pages, and add the ones the student ticked as sources.

IMPORTANT rules:
- Every route is the app's own JSON request (`native_only`), never a web page's.
- /study/find/resolve is classification only: no network.
- /study/find/add re-classifies every url here; the app's kind must agree, and only browser
  kinds may carry text. Errors are {"detail": code, "message": gentle sentence}.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from service.memory_saver_api import native_only
from service.study import api, find
from service.study.store import study_exists, study_store

router = APIRouter(prefix="/study", tags=["study"])

NO_COURSE = "Wisp couldn't find that course in Study. It may have been cleared."


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Link(_Strict):
    title: StrictStr = Field(default="", max_length=1000)
    url: StrictStr = Field(max_length=4000)
    type: StrictStr | None = Field(default=None, max_length=80)


class ResolveBody(_Strict):
    course_id: StrictStr = Field(max_length=300)
    links: list[Link] = Field(max_length=find.MAX_FOUND)


class Item(_Strict):
    url: StrictStr = Field(max_length=4000)
    title: StrictStr = Field(default="", max_length=1000)
    kind: StrictStr = Field(max_length=20)
    text: StrictStr | None = Field(default=None, max_length=find.TEXT_CAP)


class AddBody(_Strict):
    course_id: StrictStr = Field(max_length=300)
    items: list[Item] = Field(max_length=find.MAX_LINKS)


async def _body(request: Request, model):
    try:
        data = await request.json()
    except ValueError as exc:
        raise api.Refusal(422, "invalid", api.BAD_BODY) from exc
    if not isinstance(data, dict):
        raise api.Refusal(422, "invalid", api.BAD_BODY)
    try:
        return model(**data)
    except (ValidationError, TypeError) as exc:
        raise api.Refusal(422, "invalid", api.BAD_BODY) from exc


def _course(course_id: str):
    """(store, course) or a 404. Reading never creates study.db: no school data, no course."""
    if not study_exists():
        raise api.Refusal(404, "not_found", NO_COURSE)
    store = study_store()
    course = next((c for c in store.courses() if c["id"] == course_id), None)
    if course is None:
        raise api.Refusal(404, "not_found", NO_COURSE)
    return store, course


@router.post("/find/resolve", dependencies=[Depends(native_only)])
@api._answer
async def find_resolve(request: Request):
    body = await _body(request, ResolveBody)
    store, course = _course(body.course_id)
    links = [link.model_dump() for link in body.links]
    return {"items": find.resolve(links, course=course, sources=store.sources())}


@router.post("/find/add", dependencies=[Depends(native_only)])
@api._answer
async def find_add(request: Request):
    body = await _body(request, AddBody)
    store, course = _course(body.course_id)
    if any(item.kind not in find.KINDS for item in body.items):
        raise api.Refusal(422, "invalid", api.BAD_BODY)
    result = await find.add(store, course, [item.model_dump() for item in body.items])
    return result | {"state": await api._state()}
