"""Google Drive for the Study tab: list the student's Docs and Slides and export one as
plain text (docs/STUDY_TAB.md, "Drive").

IMPORTANT rules:
- Read-only. It rides the existing Google sign-in (service.gmail_source) and needs the
  drive.readonly scope that sign-in already asks for; it never asks for more.
- Only https://www.googleapis.com is contacted here (the token refresh is
  gmail_source's own call to Google's token endpoint).
- A file id is checked against a strict pattern before it reaches a URL; a search word is
  escaped before it reaches a Drive query.
- Exported text is untrusted data: returned as-is, never executed or followed.
- Never log a token or a document's text; errors carry a code and a gentle sentence only.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime

import httpx

from service import gmail_source

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
ORIGIN = "https://www.googleapis.com"
FILES = f"{ORIGIN}/drive/v3/files"
DOC = "application/vnd.google-apps.document"
SLIDES = "application/vnd.google-apps.presentation"
KINDS = {DOC: "doc", SLIDES: "slides"}
OPEN_URL = {DOC: "https://docs.google.com/document/d/{id}/edit",
            SLIDES: "https://docs.google.com/presentation/d/{id}/edit"}
FILE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
MAX_LIST = 25
QUERY_CHARS = 200
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
TOTAL_S = 30.0               # one whole Drive call, however many requests it takes
try:                         # the store's cut, so a streamed export stops where saving would
    from service.study.store import SOURCE_CHARS
except ImportError:          # an older store without sources
    SOURCE_CHARS = 400_000

NOT_CONNECTED = "Connect Google in Settings ▸ Google to add your Docs and Slides."
NO_DRIVE = "Reconnect Google in Settings ▸ Google and allow Drive to add your Docs and Slides."
EXPIRED = "Google sign-in expired. Reconnect in Settings ▸ Google."
UNREACHABLE = "Wisp couldn't reach Google Drive. Try again in a moment."
DRIVE_OFF = "Turn on the Google Drive API for Wisp's Google project, then try again."
NOT_FOUND = "Wisp couldn't find that file in your Google Drive."
TOO_LARGE = "That file is too large for Google to export as text."
BAD_ID = "That isn't a Google Docs or Slides file."
EMPTY = "That file has no text to study."

# Tests swap in an httpx.MockTransport; production leaves it None (the real network).
_transport: httpx.AsyncBaseTransport | None = None


class DriveError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _missing(message: str = NO_DRIVE) -> DriveError:
    return DriveError(409, "google_missing", message)


# --- the account ------------------------------------------------------------------

def _account() -> tuple[str | None, bool]:
    """(account, has Drive) for the Study tab.

    Study has no account picker and runs outside any bound_account() context, so there is
    no "currently bound" account to reuse. It takes the first connected account (the order
    the user connected them, so the choice is stable) whose sign-in granted drive.readonly,
    preferring one whose sign-in has not expired. With no such account it reports the first
    connected account without Drive, so the message can say "reconnect" rather than "connect".
    """
    accounts = gmail_source.connected_accounts()
    with_drive = [(email, item) for email, item in accounts.items()
                  if DRIVE_SCOPE in gmail_source.granted_scopes(email, item)]
    if with_drive:
        live = [email for email, item in with_drive if not item.get("auth_expired")]
        return (live or [with_drive[0][0]])[0], True
    return (next(iter(accounts), None), False)


def status() -> dict:
    account, drive = _account()
    if account is None:
        return {"connected": False, "drive": False, "account": None, "message": NOT_CONNECTED}
    return {"connected": True, "drive": drive, "account": account,
            "message": None if drive else NO_DRIVE}


def _drive_account() -> str:
    account, drive = _account()
    if account is None:
        raise _missing(NOT_CONNECTED)
    if not drive:
        raise _missing(NO_DRIVE)
    return account


# --- requests ---------------------------------------------------------------------

def _reasons(response: httpx.Response) -> set[str]:
    try:
        body = response.json()
    except ValueError:
        return set()
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    return {str(item.get("reason", "")).lower()
            for key in ("errors", "details") for item in (error.get(key) or [])
            if isinstance(item, dict)}


def _failure(response: httpx.Response) -> DriveError:
    """One non-200 Drive answer as a refusal. Only the status and Google's reason codes
    are read; the body is never logged or passed on."""
    code = response.status_code
    reasons = _reasons(response)
    if "exportsizelimitexceeded" in reasons:
        return DriveError(413, "too_large", TOO_LARGE)
    if code == 401:
        return _missing(EXPIRED)
    if code == 403:
        if reasons & {"insufficientpermissions", "access_token_scope_insufficient", "insufficientscopes"}:
            return _missing(NO_DRIVE)
        if reasons & {"accessnotconfigured", "service_disabled"}:
            return DriveError(502, "google_error", DRIVE_OFF)
        if reasons & {"cannotexportfile", "filenotexportable"}:
            return DriveError(422, "invalid", BAD_ID)
        return DriveError(502, "google_error", UNREACHABLE)
    if code == 404:
        return DriveError(404, "not_found", NOT_FOUND)
    return DriveError(502, "google_error", UNREACHABLE)


async def _token(http: httpx.AsyncClient) -> str:
    try:
        return await gmail_source.access_token(http)
    except gmail_source.GmailError as exc:
        if "expired" in str(exc).lower() or "missing" in str(exc).lower():
            raise _missing(EXPIRED) from None
        raise DriveError(502, "google_error", UNREACHABLE) from None


def _url(path: str = "") -> str:
    url = f"{FILES}{path}"
    if not url.startswith(f"{ORIGIN}/"):      # belt and braces: only Google's API origin
        raise DriveError(422, "invalid", BAD_ID)
    return url


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, timeout=TIMEOUT, follow_redirects=False)


async def _bounded(work):
    """Run `work(http)` as the Drive account, inside one total time limit."""
    account = _drive_account()
    try:
        with gmail_source.bound_account(account):
            async with _client() as http:
                result = await asyncio.wait_for(work(http), TOTAL_S)
                if not gmail_source.still_connected():
                    raise _missing(NOT_CONNECTED)
                return result
    except DriveError:
        raise
    except gmail_source.GmailError:
        raise _missing(NOT_CONNECTED) from None
    except (TimeoutError, httpx.HTTPError):
        raise DriveError(502, "google_error", UNREACHABLE) from None


async def _get_json(http: httpx.AsyncClient, path: str, params: list[tuple[str, str]]) -> dict:
    token = await _token(http)
    response = await http.get(_url(path), params=params, headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        raise _failure(response)
    try:
        body = response.json()
    except ValueError:
        raise DriveError(502, "google_error", UNREACHABLE) from None
    if not isinstance(body, dict):
        raise DriveError(502, "google_error", UNREACHABLE)
    return body


# --- listing ----------------------------------------------------------------------

def escape(text: str) -> str:
    """A search word as a Drive query string literal's body: backslash and quote escaped,
    control characters dropped, length capped."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).strip()[:QUERY_CHARS]
    return text.replace("\\", "\\\\").replace("'", "\\'")


def drive_query(q: str = "") -> str:
    query = f"(mimeType='{DOC}' or mimeType='{SLIDES}') and trashed=false"
    word = escape(q)
    return f"{query} and name contains '{word}'" if word else query


def _modified(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _link(value) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") else None


def _row(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    kind = KINDS.get(item.get("mimeType"))
    file_id = str(item.get("id") or "")
    if kind is None or not FILE_ID.fullmatch(file_id):
        return None
    return {"id": file_id, "name": str(item.get("name") or "Untitled"), "kind": kind,
            "modified": _modified(item.get("modifiedTime")), "url": _link(item.get("webViewLink"))}


async def list_files(q: str = "", limit: int = MAX_LIST) -> list[dict]:
    """The student's Docs and Slides, most recently changed first."""
    limit = max(1, min(int(limit), MAX_LIST))
    params = [("q", drive_query(q)), ("orderBy", "modifiedTime desc"), ("pageSize", str(limit)),
              ("fields", "files(id,name,mimeType,modifiedTime,webViewLink)")]

    async def work(http):
        return await _get_json(http, "", params)
    body = await _bounded(work)
    rows = [_row(item) for item in (body.get("files") or [])]
    return [row for row in rows if row][:limit]


# --- export -----------------------------------------------------------------------

def valid_id(file_id) -> bool:
    return isinstance(file_id, str) and FILE_ID.fullmatch(file_id) is not None


async def _export_text(http: httpx.AsyncClient, file_id: str) -> str:
    """The export as text, streamed and cut at SOURCE_CHARS (the rest is never downloaded)."""
    token = await _token(http)
    async with http.stream("GET", _url(f"/{file_id}/export"), params=[("mimeType", "text/plain")],
                           headers={"Authorization": f"Bearer {token}"}) as response:
        if response.status_code != 200:
            await response.aread()      # an error body is Google's short JSON
            raise _failure(response)
        parts: list[str] = []
        size = 0
        async for chunk in response.aiter_text():
            parts.append(chunk)
            size += len(chunk)
            if size >= SOURCE_CHARS:
                break
    return "".join(parts)[:SOURCE_CHARS]


async def export(file_id: str) -> dict:
    """{"id", "name", "kind", "url", "text"} for one Doc or Slides file."""
    if not valid_id(file_id):
        raise DriveError(422, "invalid", BAD_ID)

    async def work(http):
        meta = await _get_json(http, f"/{file_id}", [("fields", "id,name,mimeType,webViewLink,trashed"),
                                                     ("supportsAllDrives", "true")])
        mime = meta.get("mimeType")
        if mime not in KINDS or meta.get("trashed"):
            raise DriveError(422, "invalid", BAD_ID)
        text = await _export_text(http, file_id)
        return meta, mime, text
    meta, mime, text = await _bounded(work)
    text = text.lstrip("﻿")
    if not text.strip():
        raise DriveError(422, "invalid", EMPTY)
    return {"id": file_id, "name": str(meta.get("name") or "Untitled"), "kind": KINDS[mime],
            "url": _link(meta.get("webViewLink")) or OPEN_URL[mime].format(id=file_id), "text": text}
