"""The "Learn from my use" switch and its history (service/inference/use_log.py).

IMPORTANT rules:
- Writes are the app's own JSON request, never a browser page's, like "Save memory".
- Nothing here returns recorded content: only the switch, a size and a count.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StrictBool

from service.inference import use_log
from service.memory_saver_api import native_only

router = APIRouter()


class LearnFromUse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


def _state() -> dict:
    return {"enabled": use_log.enabled(), **use_log.usage()}


@router.get("/use_log")
async def get_use_log() -> dict:
    return _state()


@router.post("/use_log", dependencies=[Depends(native_only)])
async def set_use_log(body: LearnFromUse) -> dict:
    use_log.set_enabled(body.enabled)
    return _state()


@router.post("/use_log/delete", dependencies=[Depends(native_only)])
async def delete_use_log() -> dict:
    return {"deleted_bytes": use_log.delete_all(), **_state()}
