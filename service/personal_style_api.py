"""HTTP surface for "Your writing style" (service/personal_style.py).

IMPORTANT rules:
- Every write is the app's own JSON request, never a browser page's: training
  reads the person's sent mail, so a web page must not be able to start it.
- Replies are counts and fixed words only; no mail text ever appears in one.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from service import personal_style

router = APIRouter()


def _native_only(request: Request) -> None:
    """A dependency, so it refuses before the body is even parsed."""
    if "origin" in request.headers:
        raise HTTPException(403, "Change this from Wisp's settings.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")


class Switch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class Nothing(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/personal_style")
async def get_status() -> dict:
    return personal_style.status()


@router.put("/personal_style", dependencies=[Depends(_native_only)])
async def set_enabled(body: Switch) -> dict:
    return personal_style.set_enabled(body.enabled)


@router.post("/personal_style/train", dependencies=[Depends(_native_only)])
async def train(body: Nothing) -> dict:
    try:
        return personal_style.start_training()
    except personal_style.StyleError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/personal_style/stop", dependencies=[Depends(_native_only)])
async def stop(body: Nothing) -> dict:
    return await personal_style.stop()


@router.post("/personal_style/forget", dependencies=[Depends(_native_only)])
async def forget(body: Nothing) -> dict:
    erased = await personal_style.forget()
    return {**personal_style.status(), "erased": erased}
