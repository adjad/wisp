"""Local Today endpoints. No source writes, model calls, or outbound effects."""
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from service.assistant.store import assistant_store
from service.assistant.today import RevisionConflict, build_plan

router = APIRouter(prefix="/assistant/today", tags=["today"])


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    day: str
    timezone: str = "UTC"
    kind: str = "task"
    duration_minutes: StrictInt = Field(ge=1, le=1440)
    priority: StrictInt = Field(default=2, ge=1, le=3)
    due_ts: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    pinned_start: float | None = Field(default=None, strict=True, allow_inf_nan=False)


class EditTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)
    title: str | None = None
    day: str | None = None
    timezone: str | None = None
    kind: str | None = None
    duration_minutes: StrictInt | None = None
    priority: StrictInt | None = None
    due_ts: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    pinned_start: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    status: str | None = None


class Replan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: str
    timezone: str
    revision: StrictInt = Field(ge=0)
    start_minute: StrictInt = 540
    end_minute: StrictInt = 1080
    not_before: float | None = Field(default=None, strict=True, allow_inf_nan=False)


def fail(exc):
    raise HTTPException(status_code=409 if isinstance(exc, RevisionConflict) else
                        404 if isinstance(exc, KeyError) else 422, detail=str(exc)) from exc


def snapshot(day, timezone):
    state = assistant_store.today_snapshot(day, timezone)
    plan = build_plan(day, timezone, state["tasks"], state["commitments"], state["preferences"],
                      state["sources"], now=time.time())
    return {**plan, "revision": state["revision"]}


@router.get("")
def today(day: str, timezone: str):
    try:
        return snapshot(day, timezone)
    except ValueError as exc:
        fail(exc)


@router.post("/tasks", status_code=201)
def create_task(body: CreateTask):
    try:
        return assistant_store.today_save_task(body.model_dump())
    except ValueError as exc:
        fail(exc)


@router.patch("/tasks/{task_id}")
def edit_task(task_id: str, body: EditTask):
    try:
        values = body.model_dump(exclude_unset=True, exclude={"revision"})
        return assistant_store.today_save_task(values, task_id=task_id, revision=body.revision)
    except (ValueError, KeyError) as exc:
        fail(exc)


@router.post("/replan")
def replan(body: Replan):
    try:
        assistant_store.today_save_preferences(body.day, body.timezone,
            body.model_dump(exclude={"day", "timezone", "revision"}), body.revision)
        return snapshot(body.day, body.timezone)
    except ValueError as exc:
        fail(exc)
