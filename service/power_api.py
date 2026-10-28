"""Low Power Mode endpoints for the notch toggle (service/inference/power_tier.py).

IMPORTANT rules:
- The write is the app's own JSON request, never a browser page's: a cap
  throttles the whole Mac's GPU, so a web page must not be able to turn it on.
- Off is the default and the answer whenever the setting can't be read.
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


class LowPower(BaseModel):
    model_config = ConfigDict(extra="forbid")
    low_power: StrictBool


def _state() -> dict:
    return {"low_power": power_tier.low_power()}


@router.get("/power")
async def get_power() -> dict:
    return _state()


@router.post("/power", dependencies=[Depends(_native_only)])
async def set_power(body: LowPower) -> dict:
    power_tier.set_low_power(body.low_power)
    return _state()
