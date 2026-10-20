"""Endpoints for the evening check-in, carry-forward and goal pulse (service/assistant/checkin.py).

Local only. Task changes go through the store's validated save; nothing here
sends, schedules or calls a model.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

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


def _availability():
    """The Day tab's behavior-availability provider, or None when that layer is unavailable."""
    try:
        from service.assistant.daily_workspace_api import availability
    except Exception:  # noqa: BLE001
        return None
    now = time.time()
    return lambda day, timezone=None: availability(day, timezone or "UTC", now)


@router.get("/move_suggestions")
def move_suggestions(event_id: str, day: str, timezone: str = "UTC"):
    """Other times an event from the check-in could move to: deterministic,
    read-only, no model call. The agent's own confirmation still gates the move."""
    now = time.time()
    try:
        event = checkin.find_event(assistant_store, event_id, day, timezone)
        if event is None:
            raise KeyError("That event is no longer on the calendar")
        provider = _availability()
        behavior = (lambda d: provider(d, timezone)) if provider else None
        return checkin.move_suggestions(assistant_store, event, timezone, now=now, behavior=behavior)
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


class CancelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=1000)
    expected_start_ts: float = Field(gt=0, allow_inf_nan=False)
    # The count the card stated ("its N invitees will be notified"); 0 when none.
    expected_invitees: StrictInt = Field(ge=0, le=100000)


def _refuse(status: int, outcome: str, message: str):
    raise HTTPException(status, {"outcome": outcome, "message": message})


@router.post("/cancel_event")
async def cancel_event(body: CancelEvent, request: Request):
    """Cancel one exact Calendar occurrence from the Day tab's Cancel button.

    The click (after the card's Undo window) is the confirmation; no agent turn runs.
    The app removes only this occurrence (.thisEvent) after checking it is still the
    event the card showed, and reads back that it is gone. Outcomes:
    200 verified; 404 not in the synced Calendar; 403 not the user's to cancel;
    409 not current (or the outcome is unknown: detail.outcome "unknown");
    503 the app isn't connected; 502 Calendar refused or failed.
    """
    # A destructive action: only the native app's JSON request, never a browser page.
    if "origin" in request.headers:
        _refuse(403, "not_allowed", "Cancel this from Wisp's Day tab.")
    if (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower() != "application/json":
        _refuse(415, "not_allowed", "Content-Type must be application/json.")
    row = assistant_store.calendar_occurrence(body.source_id, body.expected_start_ts)
    if row is None:
        _refuse(404, "not_found", "That event is no longer in your synced Calendar; nothing was cancelled.")
    if (reason := checkin.cancel_block_reason(row)):
        _refuse(409 if row["invitee_count"] is None else 403, "not_allowed", reason)
    if row["invitee_count"] != body.expected_invitees:
        _refuse(409, "not_current", "That event's invitees changed; refresh before cancelling. Nothing was cancelled.")
    from service.assistant import outbox
    if not await outbox.app_connected():
        _refuse(503, "unavailable", "The Wisp app isn't connected, so Calendar can't be changed. Nothing was cancelled.")
    result = await outbox.request("cancel_calendar_event", {
        "source_id": row["source_id"], "expected_title": row["title"],
        "expected_start_ts": row["when_ts"], "invitee_count": row["invitee_count"]})
    if result.get("ok") is not True:
        error = str(result.get("error") or "Calendar did not confirm the cancel").rstrip(". ") + "."
        if result.get("status") == "unknown":
            _refuse(409, "unknown", "Wisp couldn't confirm whether it was cancelled. Check Calendar before trying again.")
        if "changed since" in error or "no longer at the time" in error:
            _refuse(409, "not_current", error)
        if "Only the organizer" in error or "who organizes" in error or "can't be changed" in error:
            _refuse(403, "not_allowed", error)   # the app's own refusal: permanent, never retried
        if "not connected" in error:
            _refuse(503, "unavailable", error)
        _refuse(502, "failed", error)
    from service.assistant.hub import hub
    await hub.publish({"type": "changed"})
    # invitees_notified: Calendar sends the organizer's invitees the cancellation.
    return {"ok": True, "source_id": row["source_id"], "title": row["title"],
            "invitees_notified": row["invitee_count"] > 0, "invitee_count": row["invitee_count"],
            "at": time.time()}
