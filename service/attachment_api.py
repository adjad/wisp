"""The "Read new attachments in the background" switch (service/attachment_index.py).

IMPORTANT rules:
- The write is the app's own JSON request, never a browser page's: it makes Wisp read files, so
  a web page must not be able to turn it on.
- The reply is counts and fixed tokens only; nothing from a file ever appears in it.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from service import attachment_index

router = APIRouter()


def _native_only(request: Request) -> None:
    """A dependency, so it refuses before the body is even parsed."""
    if "origin" in request.headers:
        raise HTTPException(403, "Change this from Wisp's settings.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")


class Background(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


@router.get("/assistant/attachments/background")
async def get_background() -> dict:
    return await asyncio.to_thread(attachment_index.status)


@router.put("/assistant/attachments/background", dependencies=[Depends(_native_only)])
async def set_background(body: Background) -> dict:
    await asyncio.to_thread(attachment_index.set_enabled, body.enabled)
    return await asyncio.to_thread(attachment_index.status)
