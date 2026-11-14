"""Routes for adding Google Drive Docs and Slides as Study sources (docs/STUDY_TAB.md, "Drive").

IMPORTANT rules:
- Every change is the app's own JSON request (`native_only`), never a web page's.
- Only the file the student picks is read; its text is untrusted data, saved as a source.
- Errors are {"detail": code, "message": gentle sentence} (api._answer); never a token or
  document text.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from service.memory_saver_api import native_only
from service.study import api, drive
from service.study.store import study_exists, study_store

router = APIRouter(prefix="/study/drive", tags=["study"])

NO_COURSE = "Wisp couldn't find that course in Study. It may have been cleared."


class AddBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: StrictStr = Field(max_length=300)
    course_id: StrictStr | None = Field(default=None, max_length=300)


def _refusal(exc: drive.DriveError) -> api.Refusal:
    return api.Refusal(exc.status, exc.code, exc.message)


async def _add_body(request: Request) -> AddBody:
    try:
        data = await request.json()
    except ValueError as exc:
        raise api.Refusal(422, "invalid", api.BAD_BODY) from exc
    if not isinstance(data, dict):
        raise api.Refusal(422, "invalid", api.BAD_BODY)
    try:
        return AddBody(**data)
    except (ValidationError, TypeError) as exc:
        raise api.Refusal(422, "invalid", api.BAD_BODY) from exc


@router.get("/status")
@api._answer
async def drive_status():
    return drive.status()


@router.get("/files")
@api._answer
async def drive_files(request: Request):
    q = request.query_params.get("q") or ""
    if len(q) > drive.QUERY_CHARS:
        raise api.Refusal(422, "invalid", api.BAD_BODY)
    try:
        limit = int(request.query_params.get("limit") or drive.MAX_LIST)
    except ValueError as exc:
        raise api.Refusal(422, "invalid", api.BAD_BODY) from exc
    if not 1 <= limit <= drive.MAX_LIST:
        raise api.Refusal(422, "invalid", api.BAD_BODY)
    try:
        return {"files": await drive.list_files(q, limit)}
    except drive.DriveError as exc:
        raise _refusal(exc) from None


@router.post("/add", dependencies=[Depends(native_only)])
@api._answer
async def drive_add(request: Request):
    body = await _add_body(request)
    if not drive.valid_id(body.file_id):
        raise api.Refusal(422, "invalid", drive.BAD_ID)
    course_id = body.course_id or None
    if course_id is not None:
        # Reading never creates study.db: no school data means no such course.
        courses = {c["id"] for c in study_store().courses()} if study_exists() else set()
        if course_id not in courses:
            raise api.Refusal(404, "not_found", NO_COURSE)
    try:
        found = await drive.export(body.file_id)
    except drive.DriveError as exc:
        raise _refusal(exc) from None
    summary = study_store().save_source({"kind": "drive", "url": found["url"], "title": found["name"],
                                         "text": found["text"], "course_id": course_id})
    return {"source": summary, "state": await api._state()}
