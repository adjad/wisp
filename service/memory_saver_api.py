"""The "Save memory" switch for the notch (service/memory_saver.py).

IMPORTANT rules:
- The write is the app's own JSON request, never a browser page's, like
  "Save power" (service/power_api.py).
- Off means the chat model stays resident as before; on parks it while Wisp
  isn't being used. Only the switch is stored; the parked state lives in memory.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from service import memory_saver

router = APIRouter()


def native_only(request: Request) -> None:
    """A dependency, so it refuses before the body is even parsed."""
    if "origin" in request.headers:
        raise HTTPException(403, "Change this from Wisp's notch.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")


class SaveMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    save_memory: StrictBool


@router.get("/memory_saver")
async def get_memory_saver() -> dict:
    return memory_saver.state()


@router.post("/memory_saver", dependencies=[Depends(native_only)])
async def set_memory_saver(body: SaveMemory) -> dict:
    memory_saver.set_save_memory(body.save_memory)
    return memory_saver.state()
