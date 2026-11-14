"""Routes for adding Google Drive Docs, Slides and PDFs as Study sources, read as pages when
the Study connector is the Gemini API (docs/STUDY_TAB.md, "Drive" and "Slides as pages").

IMPORTANT rules:
- Every change is the app's own JSON request (`native_only`), never a web page's.
- Only the file the student picks is read; its text is untrusted data, saved as a source.
- Errors are {"detail": code, "message": gentle sentence} (api._answer); never a token or
  document text.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from service.memory_saver_api import native_only
from service.study import api, deck, drive
from service.study.store import study_exists, study_store

log = logging.getLogger(__name__)

# One router for /study/drive/* and /study/sources/{id}/pages (service/main.py includes it).
router = APIRouter(prefix="/study", tags=["study"])

ADD_BUDGET_S = 110.0   # the whole add, under the app's 120 s request timeout
NO_SOURCE = "Wisp couldn't find that source in Study. It may have been removed."

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


@router.get("/drive/status", dependencies=[Depends(native_only)])
@api._answer
async def drive_status():
    return drive.status()


@router.get("/drive/files", dependencies=[Depends(native_only)])
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


@router.post("/drive/add", dependencies=[Depends(native_only)])
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
    started = time.monotonic()
    # Pages are read only on the Gemini API with a saved Study key; otherwise today's text-only add.
    can_read = await deck.available_async()
    try:
        found = await drive.export(body.file_id, pdf_ok=can_read)
    except drive.DriveError as exc:
        raise _refusal(exc) from None
    pages, deck_status, deck_error = await _read_pages(found, started) if can_read else (None, "none", None)
    if found["kind"] == "pdf" and not pages:
        raise api.Refusal(502 if deck_status == "failed" else 422,
                          "google_error" if deck_status == "failed" else "invalid",
                          "Wisp couldn't read that PDF. Try again in a moment." if deck_status == "failed"
                          else drive.PDF_NEEDS_GEMINI)
    text = found["text"] or _pages_text(pages or [])
    store = study_store()
    summary = store.save_source({"kind": "drive", "url": found["url"], "title": found["name"],
                                 "text": text, "course_id": course_id})
    if pages:
        store.save_pages(summary["id"], pages)
        store.set_deck_error(summary["id"], None)
    elif deck_status == "failed" and not store.pages(summary["id"]):
        store.set_deck_status(summary["id"], "failed")   # a failed re-read keeps the earlier pages and status
        store.set_deck_error(summary["id"], deck_error)
    summary = next((s for s in store.sources() if s["id"] == summary["id"]), summary)
    return {"source": summary, "state": await api._state()}


TOO_BIG_DECK = "The file is over 14 MB, too large to read as slides."
TOO_SLOW_DECK = "Reading the slides took too long. Try again in a moment."


async def _read_pages(found: dict, started: float) -> tuple[list[dict] | None, str, str | None]:
    """(pages, deck_status, reason) for one file: "ready" with pages, else "failed" (too large, Google
    refused the PDF export, or Gemini failed). Never raises: the text source still saves."""
    try:
        data = await drive.pdf_bytes(found["id"], found["kind"])
    except drive.DriveError as exc:
        log.info("Study deck: PDF fetch refused (%s)", exc.code)
        return None, "failed", exc.message
    if data is None:
        log.info("Study deck: PDF over the inline cap; kept text-only")
        return None, "failed", TOO_BIG_DECK
    remaining = ADD_BUDGET_S - (time.monotonic() - started)
    if remaining < 10:
        return None, "failed", TOO_SLOW_DECK
    try:
        pages = await deck.read_pages(data, title=found["name"], timeout_s=min(deck.TIMEOUT_S, remaining))
    except deck.DeckError as exc:
        log.info("Study deck: read failed (%s)", exc.code)
        return None, "failed", str(exc)
    if not pages:
        return None, "none", None
    return pages, "ready", None


def _pages_text(pages: list[dict]) -> str:
    """A PDF's source text, built from its pages (it has no text export)."""
    lines = []
    for p in pages:
        lines.append(f"Page {p['page']}: {p['title']}")
        lines += [f"- {point}" for point in p["points"]]
        if p.get("figure"):
            lines.append(f"Figure: {p['figure']}")
    return "\n".join(lines)


@router.get("/sources/{source_id}/pages", dependencies=[Depends(native_only)])
@api._answer
async def source_pages(source_id: str):
    store = study_store() if study_exists() else None
    if store is None or len(source_id) > 64 or store.source(source_id) is None:
        raise api.Refusal(404, "not_found", NO_SOURCE)
    return {"pages": store.pages(source_id)}
