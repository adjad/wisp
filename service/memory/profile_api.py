"""HTTP surface for the user's profile (service/memory/profile.py).

The notch's Getting to know you flow PATCHes after every step, so closing the
panel mid-way loses nothing; About you in the Memory window edits the same
fields. Every response carries the profile, its status and the preview of how
Wisp uses it, so a client never needs a second call to show the result.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from service.memory import profile as P

router = APIRouter(prefix="/profile", tags=["profile"])


def _payload(profile: dict) -> dict[str, Any]:
    return {"profile": profile, "status": P.status(profile), "preview": P.preview(profile)}


@router.get("")
def get_profile() -> dict[str, Any]:
    return _payload(P.load())


@router.patch("")
def patch_profile(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return _payload(P.update(body))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.post("/complete")
def complete_profile() -> dict[str, Any]:
    return _payload(P.mark_complete())


@router.post("/dismiss")
def dismiss_profile() -> dict[str, Any]:
    return _payload(P.dismiss())


@router.delete("")
def delete_profile() -> dict[str, Any]:
    return _payload(P.clear())
