"""The "Save power" switch for the notch (service/inference/power_tier.py).

IMPORTANT rules:
- The write is the app's own JSON request, never a browser page's: a cap
  throttles the whole Mac's GPU, so a web page must not be able to change it.
- The switch gates every cap: chat replies and Brief generation on battery,
  the Brief while the user is away, and background work always. Off means none.
- The stored and wire key stays `save_when_away` (the switch's first name), so
  an existing choice survives the rename.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from service.inference import power_tier

router = APIRouter()


def _native_only(request: Request) -> None:
    """A dependency, so it refuses before the body is even parsed."""
    if "origin" in request.headers:
        raise HTTPException(403, "Change this from Wisp's notch.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")


class SaveWhenAway(BaseModel):
    model_config = ConfigDict(extra="forbid")
    save_when_away: StrictBool


def _state() -> dict:
    return {"save_when_away": power_tier.save_when_away()}


@router.get("/power")
async def get_power() -> dict:
    return _state()


@router.post("/power", dependencies=[Depends(_native_only)])
async def set_power(body: SaveWhenAway) -> dict:
    power_tier.set_save_when_away(body.save_when_away)
    return _state()
