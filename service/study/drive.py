"""Google Drive for the Study tab: list the student's Docs, Slides and PDFs, export a Doc or
Slides file as plain text, and fetch any of them as one PDF for reading as pages
(docs/STUDY_TAB.md, "Drive" and "Slides as pages").

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
import logging
import re
from datetime import datetime

import httpx

from service import gmail_source

log = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
ORIGIN = "https://www.googleapis.com"
FILES = f"{ORIGIN}/drive/v3/files"
DOC = "application/vnd.google-apps.document"
SLIDES = "application/vnd.google-apps.presentation"
PDF = "application/pdf"
KINDS = {DOC: "doc", SLIDES: "slides", PDF: "pdf"}
OPEN_URL = {DOC: "https://docs.google.com/document/d/{id}/edit",
            SLIDES: "https://docs.google.com/presentation/d/{id}/edit",
            PDF: "https://drive.google.com/file/d/{id}/view"}
PDF_BYTES = 18 * 1024 * 1024  # a deck read as pages is sent inline; over this it stays text-only
PDF_TOTAL_S = 45.0            # one PDF export or download
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
UNREACHABLE = "Wisp couldn't reach Google Drive. Check your connection and try again."
TIMED_OUT = "Google Drive took too long to answer. Try again in a moment."
DRIVE_OFF = ("Google Drive isn't turned on for Wisp's Google project yet. Turn on the Google Drive API "
             "in Google Cloud, then try again.")
REFUSED = "Google Drive refused that request. Check that your Google account can open the file, then try again."
RATE_LIMITED = "Google Drive is busy right now. Try again in a minute."
GOOGLE_DOWN = "Google Drive is having trouble right now. Try again in a few minutes."
ODD_REPLY = "Google Drive sent an answer Wisp couldn't use. Try again in a moment."
NOT_FOUND = "Wisp couldn't find that file in your Google Drive."
TOO_LARGE = "That file is too large for Google to export as text."
BAD_ID = "That isn't a Google Docs, Slides or PDF file."
PDF_NEEDS_GEMINI = "Reading a PDF needs the Gemini API as the Study connector. Choose it in Study settings."
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

_REASON = re.compile(r"[a-z0-9_.]{1,60}")
_DRIVE_OFF_TEXT = ("has not been used in project", "is disabled")


def _error(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _reasons(response: httpx.Response) -> set[str]:
    """Google's reason codes, lower-cased; anything that is not a plain code is dropped,
    so a reason can be logged without carrying text from the body."""
    error = _error(response)
    reasons = {str(item.get("reason", "")).lower()
               for key in ("errors", "details") for item in (error.get(key) or [])
               if isinstance(item, dict)}
    return {r for r in reasons if _REASON.fullmatch(r)}


def _says_drive_off(response: httpx.Response) -> bool:
    """Google's message for an API not enabled in the OAuth client's project. Only searched,
    never logged or passed on."""
    error = _error(response)
    message = error.get("message")
    texts = [message] if isinstance(message, str) else []
    texts += [item.get("message") for item in (error.get("errors") or [])
              if isinstance(item, dict) and isinstance(item.get("message"), str)]
    return any(marker in text.lower() for text in texts for marker in _DRIVE_OFF_TEXT)


def _logged(error: "DriveError", status, reasons=()) -> "DriveError":
    """One log line per Drive failure: the HTTP status (or the failure kind) and Google's
    reason codes only. Never a body, token, file id or file name."""
    log.warning("Study Drive call failed: status=%s reasons=%s -> %s", status,
                ",".join(sorted(reasons)[:5]) or "-", error.code)
    return error


def _failure(response: httpx.Response) -> DriveError:
    """One non-200 Drive answer as a refusal. Only the status and Google's reason codes
    are read (and Google's message, searched for the Drive-off wording); the body is never
    logged or passed on."""
    code = response.status_code
    reasons = _reasons(response)
    return _logged(_refusal_for(code, reasons, response), code, reasons)


def _refusal_for(code: int, reasons: set[str], response: httpx.Response) -> DriveError:
    if "exportsizelimitexceeded" in reasons:
        return DriveError(413, "too_large", TOO_LARGE)
    if code == 401:
        return _missing(EXPIRED)
    if code == 403:
        if reasons & {"accessnotconfigured", "service_disabled"} or _says_drive_off(response):
            return DriveError(409, "drive_off", DRIVE_OFF)
        if reasons & {"insufficientpermissions", "access_token_scope_insufficient", "insufficientscopes"}:
            return _missing(NO_DRIVE)
        if reasons & {"cannotexportfile", "filenotexportable"}:
            return DriveError(422, "invalid", BAD_ID)
        if reasons & {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded"}:
            return DriveError(502, "google_error", RATE_LIMITED)
        return DriveError(502, "google_error", REFUSED)
    if code == 404:
        return DriveError(404, "not_found", NOT_FOUND)
    if code == 429:
        return DriveError(502, "google_error", RATE_LIMITED)
    if code >= 500:
        return DriveError(502, "google_error", GOOGLE_DOWN)
    return DriveError(502, "google_error", ODD_REPLY)


async def _token(http: httpx.AsyncClient) -> str:
    try:
        return await gmail_source.access_token(http)
    except gmail_source.GmailError as exc:
        if "expired" in str(exc).lower() or "missing" in str(exc).lower():
            raise _logged(_missing(EXPIRED), "token") from None
        raise _logged(DriveError(502, "google_error", UNREACHABLE), "token") from None


def _url(path: str = "") -> str:
    url = f"{FILES}{path}"
    if not url.startswith(f"{ORIGIN}/"):      # belt and braces: only Google's API origin
        raise DriveError(422, "invalid", BAD_ID)
    return url


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, timeout=TIMEOUT, follow_redirects=False)


async def _bounded(work, total_s: float = TOTAL_S):
    """Run `work(http)` as the Drive account, inside one total time limit."""
    account = _drive_account()
    try:
        with gmail_source.bound_account(account):
            async with _client() as http:
                result = await asyncio.wait_for(work(http), total_s)
                if not gmail_source.still_connected():
                    raise _missing(NOT_CONNECTED)
                return result
    except DriveError:
        raise
    except gmail_source.GmailError:
        raise _logged(_missing(NOT_CONNECTED), "account") from None
    except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
        raise _logged(DriveError(502, "google_error", TIMED_OUT), "timeout") from None
    except httpx.HTTPError:
        raise _logged(DriveError(502, "google_error", UNREACHABLE), "unreachable") from None


async def _get_json(http: httpx.AsyncClient, path: str, params: list[tuple[str, str]]) -> dict:
    token = await _token(http)
    response = await http.get(_url(path), params=params, headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        raise _failure(response)
    try:
        body = response.json()
    except ValueError:
        raise _logged(DriveError(502, "google_error", ODD_REPLY), "bad_json") from None
    if not isinstance(body, dict):
        raise _logged(DriveError(502, "google_error", ODD_REPLY), "bad_json")
    return body


# --- listing ----------------------------------------------------------------------

def escape(text: str) -> str:
    """A search word as a Drive query string literal's body: backslash and quote escaped,
    control characters dropped, length capped."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).strip()[:QUERY_CHARS]
    return text.replace("\\", "\\\\").replace("'", "\\'")


def drive_query(q: str = "") -> str:
    query = f"(mimeType='{DOC}' or mimeType='{SLIDES}' or mimeType='{PDF}') and trashed=false"
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
    """The student's Docs, Slides and PDFs, most recently changed first."""
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


async def _meta(http: httpx.AsyncClient, file_id: str) -> tuple[dict, str]:
    meta = await _get_json(http, f"/{file_id}", [("fields", "id,name,mimeType,webViewLink,trashed"),
                                                 ("supportsAllDrives", "true")])
    mime = meta.get("mimeType")
    if mime not in KINDS or meta.get("trashed"):
        raise DriveError(422, "invalid", BAD_ID)
    return meta, mime


def _found(file_id: str, meta: dict, mime: str, text: str) -> dict:
    return {"id": file_id, "name": str(meta.get("name") or "Untitled"), "kind": KINDS[mime],
            "url": _link(meta.get("webViewLink")) or OPEN_URL[mime].format(id=file_id), "text": text}


async def export(file_id: str, *, pdf_ok: bool = False) -> dict:
    """{"id", "name", "kind", "url", "text"} for one Doc or Slides file. A PDF has no text
    export: with ``pdf_ok`` it comes back with "text": "" (its pages are its text), else it
    is refused (PDF_NEEDS_GEMINI)."""
    if not valid_id(file_id):
        raise DriveError(422, "invalid", BAD_ID)

    async def work(http):
        meta, mime = await _meta(http, file_id)
        if mime == PDF:
            if not pdf_ok:
                raise DriveError(422, "invalid", PDF_NEEDS_GEMINI)
            return meta, mime, ""
        text = await _export_text(http, file_id)
        return meta, mime, text
    meta, mime, text = await _bounded(work)
    if mime == PDF:
        return _found(file_id, meta, mime, "")
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise DriveError(422, "invalid", EMPTY)
    return _found(file_id, meta, mime, text)


async def _stream_bytes(http: httpx.AsyncClient, path: str, params: list[tuple[str, str]],
                        cap: int) -> bytes | None:
    """The body, or None when it is larger than ``cap`` (the rest is never downloaded)."""
    token = await _token(http)
    async with http.stream("GET", _url(path), params=params,
                           headers={"Authorization": f"Bearer {token}"}) as response:
        if response.status_code != 200:
            await response.aread()
            raise _failure(response)
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            return None
        parts: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > cap:
                return None
            parts.append(chunk)
    return b"".join(parts)


async def pdf_bytes(file_id: str, kind: str, *, cap: int = PDF_BYTES,
                    total_s: float = PDF_TOTAL_S) -> bytes | None:
    """One file as a PDF: Docs and Slides exported as application/pdf, a PDF downloaded
    (alt=media). None when it is over ``cap``; DriveError for Google's refusals (an export
    over Google's own limit is "too_large")."""
    if not valid_id(file_id) or kind not in KINDS.values():
        raise DriveError(422, "invalid", BAD_ID)

    async def work(http):
        if kind == "pdf":
            return await _stream_bytes(http, f"/{file_id}", [("alt", "media"), ("supportsAllDrives", "true")],
                                       cap)
        return await _stream_bytes(http, f"/{file_id}/export", [("mimeType", PDF)], cap)
    return await _bounded(work, total_s)
