"""HTTP surface for meeting notes (service/meetings.py).

IMPORTANT rules:
- Every write is the app's own JSON request, never a browser page's: starting a
  meeting makes the Mac listen, so a web page must not be able to do it.
- Audio arrives as base64 16-bit PCM WAV windows of at most 30 s and is never
  stored. Replies carry transcript text and notes only. GET /meetings says where
  the audio goes (`audio_destination`: None on a local engine, else the provider).
"""
from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from service import meetings, voice

router = APIRouter()

_MAX_B64 = (int(voice.MAX_SECONDS * 16_000 * 2) + 4096) * 4 // 3 + 8


def _native_only(request: Request) -> None:
    """A dependency, so it refuses before the body is even parsed."""
    if "origin" in request.headers:
        raise HTTPException(403, "Start meetings from the notch.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: StrictStr = ""


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: StrictInt
    audio: StrictStr


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _refuse(error: Exception) -> HTTPException:
    if isinstance(error, voice.VoiceUnavailable):
        return HTTPException(409, str(error).replace("Voice works", "Meeting notes work")
                             .replace("Voice needs", "Meeting notes need").replace("Voice", "Meeting notes"))
    return HTTPException(400, str(error))


def audio_destination() -> str | None:
    """Who hears a meeting's audio besides this Mac: the remote-only profile's
    provider ("OpenRouter", or the server's host name), or None on a local engine."""
    from urllib.parse import urlsplit
    from service.inference.remote_policy import remote_only_profile
    profile = remote_only_profile()
    if profile is None:
        return None
    host = (urlsplit(profile.base_url).hostname or "").lower()
    return "OpenRouter" if host == "openrouter.ai" or host.endswith(".openrouter.ai") else (host or "your cloud provider")


@router.get("/meetings")
async def list_meetings() -> dict:
    return {"meetings": await meetings.listing(), "audio_destination": audio_destination()}


@router.get("/meetings/{meeting_id}")
async def get_meeting(meeting_id: str) -> dict:
    try:
        return await meetings.get(meeting_id)
    except meetings.MeetingError as error:
        raise HTTPException(404, str(error)) from None


@router.post("/meetings", dependencies=[Depends(_native_only)])
async def start_meeting(body: Start) -> dict:
    try:
        return await meetings.start(body.title)
    except (meetings.MeetingError, voice.VoiceUnavailable) as error:
        raise _refuse(error) from None


@router.post("/meetings/{meeting_id}/windows", dependencies=[Depends(_native_only)])
async def add_window(meeting_id: str, body: Window) -> dict:
    if len(body.audio) > _MAX_B64:
        raise HTTPException(400, "Wisp couldn't read that recording.")
    try:
        data = base64.b64decode(body.audio, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Wisp couldn't read that recording.") from None
    try:
        return await meetings.add_window(meeting_id, body.seq, data)
    except meetings.MeetingError as error:
        raise _refuse(error) from None


@router.post("/meetings/{meeting_id}/finish", dependencies=[Depends(_native_only)])
async def finish_meeting(meeting_id: str, body: Empty) -> dict:
    try:
        return await meetings.finish(meeting_id)
    except meetings.MeetingError as error:
        raise _refuse(error) from None


@router.post("/meetings/{meeting_id}/actions/{index}/reminder", dependencies=[Depends(_native_only)])
async def remind(meeting_id: str, index: int, body: Empty) -> dict:
    try:
        return await meetings.remind(meeting_id, index)
    except meetings.MeetingError as error:
        raise _refuse(error) from None


@router.delete("/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str, request: Request) -> dict:
    if "origin" in request.headers:
        raise HTTPException(403, "Delete meetings from the notch.")
    try:
        await meetings.delete(meeting_id)
    except meetings.MeetingError as error:
        raise HTTPException(404, str(error)) from None
    return {"deleted": True}
