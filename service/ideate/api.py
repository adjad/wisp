"""HTTP surface for Ideate (the contract in docs/ideate/PLAN.md, consumed by IdeateModel.swift).

IMPORTANT rules (the same as meetings_api / study):
- Every write is the app's own JSON request (`native_only`), never a web page's: a start
  runs a coding agent on this Mac. The service-wide LocalOriginGuard and AppKeyGuard apply too.
- Reads carry no secrets: events hold what the agent said and did, paths are project-relative.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from service.ideate import config, runner, store
from service.memory_saver_api import native_only

router = APIRouter(prefix="/ideate", tags=["ideate"])


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: StrictStr = Field(min_length=1, max_length=config.PROMPT_CHARS)
    kind: StrictStr = "general"
    name: StrictStr | None = Field(default=None, max_length=80)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr = Field(min_length=1, max_length=config.PROMPT_CHARS)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: StrictBool


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _manager() -> runner.Manager:
    manager = runner.manager()
    try:
        bind = getattr(manager.residency, "bind_loop", None)
        if bind is not None:
            bind(asyncio.get_running_loop())
    except RuntimeError:
        pass
    return manager


def _refuse(error: Exception) -> HTTPException:
    if isinstance(error, runner.NotFound):
        return HTTPException(404, "That idea isn't here any more.")
    if isinstance(error, (runner.Busy, runner.Unavailable)):
        return HTTPException(409, str(error))
    return HTTPException(400, "Ideate couldn't do that. Try again.")


async def _status(manager: runner.Manager) -> dict:
    install, _target, reason = await asyncio.to_thread(manager.preflight)
    out = {"available": False, "reason": reason, "harness": install.name if install else "",
           "harness_version": install.version if install else "", "harness_source": install.source if install else "",
           "model": config.MODEL_LABEL, "model_id": config.MODEL_ID, "model_loaded": False,
           "projects_root": str(manager.store.root), "busy": manager.busy_with() is not None}
    if reason:
        return out
    state = await manager.residency.state()
    out["model_loaded"] = state.loaded
    if not state.reachable:
        out["reason"] = "Wisp's engine isn't answering yet. Try again in a moment."
    elif not state.listed:
        out["reason"] = (f"{config.MODEL_LABEL} isn't downloaded yet. Open Settings and download it, "
                         "then come back.")
    else:
        out["available"] = True
    return out


@router.get("/status")
async def status() -> dict:
    return await _status(_manager())


@router.get("/projects")
async def list_projects() -> dict:
    manager = _manager()
    return {"projects": await asyncio.to_thread(manager.store.list)}


@router.post("/projects", dependencies=[Depends(native_only)])
async def start_project(body: Start) -> dict:
    manager = _manager()
    if body.kind not in store.KINDS:
        raise HTTPException(400, "Unknown kind of idea.")
    current = await _status(manager)
    if not current["available"]:
        raise HTTPException(409, current["reason"] or "Ideate isn't available right now.")
    try:
        project = await asyncio.to_thread(manager.start_new, body.prompt.strip(), body.kind, body.name)
        return await asyncio.to_thread(manager.view, project.id, 0)
    except (runner.Busy, runner.Unavailable, runner.NotFound) as exc:
        raise _refuse(exc) from None


@router.get("/projects/{project_id}")
async def get_project(project_id: str, after: int = Query(0, ge=0)) -> dict:
    try:
        return await asyncio.to_thread(_manager().view, project_id, after)
    except runner.NotFound as exc:
        raise _refuse(exc) from None


@router.post("/projects/{project_id}/message", dependencies=[Depends(native_only)])
async def message(project_id: str, body: Message) -> dict:
    manager = _manager()
    current = await _status(manager)
    if not current["available"]:
        raise HTTPException(409, current["reason"] or "Ideate isn't available right now.")
    try:
        await asyncio.to_thread(manager.message, project_id, body.text.strip())
        return await asyncio.to_thread(manager.view, project_id, 0)
    except (runner.Busy, runner.Unavailable, runner.NotFound) as exc:
        raise _refuse(exc) from None


@router.post("/projects/{project_id}/cancel", dependencies=[Depends(native_only)])
async def cancel(project_id: str, body: Empty) -> dict:
    manager = _manager()
    try:
        await asyncio.to_thread(manager.cancel, project_id)
        return await asyncio.to_thread(manager.view, project_id, 0)
    except runner.NotFound as exc:
        raise _refuse(exc) from None


@router.post("/projects/{project_id}/approvals/{approval_id}", dependencies=[Depends(native_only)])
async def answer(project_id: str, approval_id: str, body: Answer) -> dict:
    manager = _manager()
    try:
        await asyncio.to_thread(manager.answer, project_id, approval_id, body.allow)
        return {"ok": True}
    except runner.NotFound:
        raise HTTPException(404, "That request isn't waiting any more.") from None
