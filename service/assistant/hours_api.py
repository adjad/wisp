"""Routes for the person's working and quiet hours (service/user_hours.py) and the
offer to move an event outside them (service/assistant/hours_offers.py).

Local only, no model. Nothing here changes a calendar: accepting an offer only
records that it was taken to Ask, where the move has its own confirmation.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from service import user_hours
from service.assistant import hours_offers
from service.assistant.store import assistant_store

router = APIRouter(prefix="/assistant/hours", tags=["hours"])


class Working(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_minute: StrictInt = Field(ge=0, le=1439)
    end_minute: StrictInt = Field(ge=0, le=1440)
    days: list[StrictInt] | None = Field(default=None, max_length=7)


class Quiet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_minute: StrictInt = Field(ge=0, le=1439)
    end_minute: StrictInt = Field(ge=0, le=1439)


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    working: Working | None = None
    quiet: Quiet | None = None
    offer_moves: StrictBool | None = None
    # When the Brief updates. Empty goes back to the default day; user_hours checks the rest.
    brief_times: list[StrictInt] | None = Field(default=None, max_length=user_hours.MAX_BRIEF_TIMES)
    # The app's own zone, so today's plan picks up new hours at once.
    timezone: str | None = Field(default=None, max_length=100)


class OfferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str = Field(min_length=1, max_length=100)


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _body(request: Request, model):
    """The app's own JSON requests only: any Origin header (a web page, even "null")
    is refused, and so is a body that isn't JSON."""
    if "origin" in request.headers:
        raise HTTPException(403, "Use Wisp for this.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")
    try:
        data = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "Body must be a JSON object.") from exc
    if not isinstance(data, dict):
        raise HTTPException(422, "Body must be a JSON object.")
    try:
        return model(**data)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid request.") from exc


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise HTTPException(422, "Unknown timezone.") from exc


def _availability():
    """The Day tab's learned busy windows, when that layer is available."""
    try:
        from service.assistant.daily_workspace_api import availability
    except Exception:  # noqa: BLE001
        return None
    now = time.time()
    return lambda day, timezone=None: availability(day, timezone or "UTC", now)


async def _changed() -> None:
    from service.assistant.hub import hub
    await hub.publish({"type": "changed"})


@router.get("")
def get_hours() -> dict:
    return user_hours.snapshot()


@router.patch("")
async def patch_hours(request: Request) -> dict:
    body = await _body(request, Patch)
    zone = _zone(body.timezone) if body.timezone else None
    try:
        result = user_hours.save(working=body.working.model_dump() if body.working else None,
                                 quiet=body.quiet.model_dump() if body.quiet else None,
                                 offer_moves=body.offer_moves, brief_times=body.brief_times)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.working and zone is not None:
        # Days already saved keep their own hours otherwise, and today would look
        # as if the change hadn't worked until tomorrow.
        start, end = user_hours.working_hours().day_span()
        assistant_store.today_apply_working_hours(datetime.now(zone).date().isoformat(), start, end)
    await _changed()
    return result


@router.post("/offer")
async def offer(request: Request) -> dict:
    body = await _body(request, OfferRequest)
    _zone(body.timezone)
    # Plans several days to find a free slot, so not on the event loop.
    return {"offer": await asyncio.to_thread(hours_offers.next_offer, assistant_store, body.timezone,
                                             now=time.time(), behavior=_availability())}


def _offer_id(value: str) -> str:
    if len(value) != 24 or any(ch not in "0123456789abcdef" for ch in value):
        raise HTTPException(422, "Invalid id.")
    return value


@router.post("/offer/{offer_id}/dismiss")
async def dismiss(offer_id: str, request: Request) -> dict:
    await _body(request, Empty)
    return {"ok": hours_offers.dismiss(_offer_id(offer_id), now=time.time())}


@router.post("/offer/{offer_id}/accept")
async def accept(offer_id: str, request: Request) -> dict:
    await _body(request, Empty)
    return {"ok": hours_offers.accept(_offer_id(offer_id), now=time.time())}
