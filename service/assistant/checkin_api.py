"""Endpoints for the evening check-in, carry-forward and goal pulse (service/assistant/checkin.py).

Local only. Task changes go through the store's validated save; nothing here
sends, schedules or calls a model.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from service.assistant import checkin
from service.assistant.store import assistant_store
from service.assistant.today import RevisionConflict

router = APIRouter(prefix="/assistant/checkin", tags=["checkin"])


def _profile():
    try:
        from service.memory import profile
        return profile.load()
    except Exception:  # noqa: BLE001
        return None


class TaskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    day: str
    timezone: str


class DayBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: str
    timezone: str = "UTC"


class GoalStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    status: str


def _fail(exc):
    raise HTTPException(status_code=409 if isinstance(exc, RevisionConflict) else
                        404 if isinstance(exc, KeyError) else 422, detail=str(exc))


@router.get("")
def get_checkin(day: str, timezone: str = "UTC"):
    now = time.time()
    try:
        # Opening Today in the morning is as good as the scheduler's tick.
        if checkin.local_day(now, timezone) == day:
            checkin.carry_forward(assistant_store, day, timezone, now=now)
        return checkin.build(assistant_store, day, timezone, now=now, profile=_profile())
    except (ValueError, KeyError) as exc:
        _fail(exc)


@router.post("/tasks/{task_id}")
def decide(task_id: str, body: TaskDecision):
    now = time.time()
    try:
        checkin.decide_task(assistant_store, task_id, body.decision, day=body.day, timezone=body.timezone, now=now)
        return checkin.build(assistant_store, body.day, body.timezone, now=now, profile=_profile())
    except (ValueError, KeyError) as exc:
        _fail(exc)


@router.post("/complete")
def complete(body: DayBody):
    now = time.time()
    checkin.complete(body.day, now=now)
    try:
        return checkin.build(assistant_store, body.day, body.timezone, now=now, profile=_profile())
    except (ValueError, KeyError) as exc:
        _fail(exc)


@router.post("/carry_forward")
def carry_forward(body: DayBody):
    try:
        moved = checkin.carry_forward(assistant_store, body.day, body.timezone, now=time.time())
    except ValueError as exc:
        _fail(exc)
    return {"moved": [{"id": t["id"], "title": t["title"], "from": t.get("carried_from")} for t in moved]}


@router.post("/goal")
def goal(body: GoalStatus):
    try:
        from service.memory import profile
        saved = profile.set_goal_status(body.goal, body.status, now=time.time())
    except ValueError as exc:
        _fail(exc)
    return {"goal_status": saved.get("goal_status", {}), "goal_pulse": checkin.goal_pulse(saved, now=time.time())}
