"""Read-only Gmail API source: mail without Mail.app having to be open.

Mail.app's local index only moves while Mail runs and fetches, so "newer
messages may be missing; open Mail" was the best the Swift reader could do.
This module reads the inbox straight from Google instead, over the Gmail REST
API with the read-only scope, and feeds the SAME three caches the Swift
MailReader fills (`_headers`, `_history`, `_raw_emails`) through
`email_tools.cache_external_emails`, so every downstream tool is unchanged.

Setup is user-owned: Wisp cannot ship a Google OAuth client (the Gmail scopes
are restricted), so the user creates a Desktop-type client in their own Google
Cloud project and gives Wisp its id/secret once (`/assistant/gmail/connect`
or scripts/gmail_connect.py). Consent happens in the browser over a loopback
redirect with PKCE. Only `gmail.readonly` is ever requested — nothing here can
send, delete, or modify mail — and data only flows Google -> this Mac.

The refresh token lives in ~/.moe/gmail.json (0600), same trust model as the
other personal caches under ~/.moe.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
from email.header import decode_header, make_header
from email.utils import parseaddr
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from service.paths import MOE_DIR

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"

CONFIG_PATH = MOE_DIR / "gmail.json"

HEADERS_LIMIT = 200          # per account, same depth as MailReader
RAW_LIMIT = 50
HISTORY_LIMIT = 2000         # ~2y cap; first walk is the only expensive one
HISTORY_DAYS = 730
BODY_CAP = 20000
FETCH_CONCURRENCY = 4        # metadata get = 5 quota units; the cap is 250/s

HEADERS_EVERY = 300
RAW_EVERY = 900
HISTORY_EVERY = 1800

_FS = "\x01"
_RS = "\x02"

_meta: dict[str, dict] = {}      # message id -> parsed metadata (immutable)
_last = {"headers": 0.0, "raw": 0.0, "history": 0.0}
_last_error = ""
_lock: asyncio.Lock | None = None
_pending_flow: dict | None = None


# --- config / token -----------------------------------------------------------

def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — absent/corrupt means "not connected"
        return {}


def _save_config(cfg: dict) -> None:
    MOE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle)
    tmp.replace(CONFIG_PATH)


def is_connected() -> bool:
    cfg = load_config()
    return bool(cfg.get("refresh_token") and cfg.get("client_id") and cfg.get("email"))


def status() -> dict:
    cfg = load_config()
    return {"connected": is_connected(), "email": cfg.get("email", ""),
            "last_headers_sync": _last["headers"] or None,
            "last_error": _last_error,
            "authorizing": _pending_flow is not None}


_access: dict = {"token": "", "expires": 0.0}


async def _access_token(http: httpx.AsyncClient) -> str:
    if _access["token"] and time.time() < _access["expires"] - 60:
        return _access["token"]
    cfg = load_config()
    response = await http.post(TOKEN_URL, data={
        "client_id": cfg.get("client_id", ""),
        "client_secret": cfg.get("client_secret", ""),
        "refresh_token": cfg.get("refresh_token", ""),
        "grant_type": "refresh_token"})
    body = response.json() if response.content else {}
    if response.status_code != 200 or "access_token" not in body:
        # invalid_grant = revoked/expired consent (Google "Testing" apps expire
        # refresh tokens after 7 days) — the user has to reconnect.
        raise GmailError(f"token refresh failed: {body.get('error', response.status_code)}")
    _access["token"] = body["access_token"]
    _access["expires"] = time.time() + float(body.get("expires_in", 3600))
    return _access["token"]


class GmailError(RuntimeError):
    pass


async def _get(http: httpx.AsyncClient, path: str, params: dict | list | None = None) -> dict:
    for attempt in range(4):
        token = await _access_token(http)
        response = await http.get(f"{API}/{path}", params=params,
                                  headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            _access["token"] = ""
            continue
        if response.status_code in (429, 403, 500, 503) and attempt < 3:
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        if response.status_code != 200:
            raise GmailError(f"{path}: HTTP {response.status_code}")
        return response.json()
    raise GmailError(f"{path}: unauthorized")


# --- message parsing ----------------------------------------------------------

def _clean(value: str) -> str:
    return re.sub(r"[\x01\x02\r\n]+", " ", value or "").strip()


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:  # noqa: BLE001 — malformed encoded-words fall back to raw
        return value or ""


def _header(payload: dict, name: str) -> str:
    for item in (payload or {}).get("headers", []):
        if item.get("name", "").lower() == name.lower():
            return item.get("value", "")
    return ""


def parse_metadata(message: dict) -> dict:
    payload = message.get("payload") or {}
    name, address = parseaddr(_decode(_header(payload, "From")))
    return {"id": message.get("id", ""),
            "ts": int(message.get("internalDate", 0)) / 1000.0,
            "name": _clean(name), "address": _clean(address),
            "to": _clean(_decode(_header(payload, "To"))),
            "subject": _clean(_decode(_header(payload, "Subject"))),
            "message_id": _clean(_header(payload, "Message-ID")),
            "unread": "UNREAD" in (message.get("labelIds") or [])}


def header_line(meta: dict, account: str, unread: bool) -> str:
    # Same H2 record MailReader.swift emits (10 fields).
    return _FS.join(["H2", str(int(meta["ts"])), "U" if unread else "R", account,
                     account, meta["name"], meta["address"], meta["message_id"],
                     meta["subject"], meta["id"]])


def _b64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def extract_body(payload: dict) -> str:
    plain: list[str] = []
    markup: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            markup.append(_b64(data))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    if plain:
        text = "\n".join(plain)
    else:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(markup))
        text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", text.replace("\x01", " ").replace("\x02", " "))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()[:BODY_CAP]


def raw_record(message: dict, account: str) -> str:
    meta = parse_metadata(message)
    sender = f"{meta['name']} <{meta['address']}>" if meta["name"] else meta["address"]
    return _FS.join([str(int(meta["ts"])), "U" if meta["unread"] else "R", account, account,
                     sender, meta["to"], meta["subject"], meta["message_id"],
                     extract_body(message.get("payload") or {})])


# --- syncing ------------------------------------------------------------------

async def _list_ids(http, limit: int, *, query: str = "", unread: bool = False) -> list[str]:
    ids: list[str] = []
    page = ""
    while len(ids) < limit:
        params: list[tuple[str, str]] = [("labelIds", "INBOX"),
                                         ("maxResults", str(min(500, limit - len(ids))))]
        if unread:
            params.append(("labelIds", "UNREAD"))
        if query:
            params.append(("q", query))
        if page:
            params.append(("pageToken", page))
        data = await _get(http, "messages", params)
        ids.extend(item["id"] for item in data.get("messages", []))
        page = data.get("nextPageToken", "")
        if not page:
            break
    return ids[:limit]


async def _ensure_meta(http, ids: list[str]) -> None:
    missing = [i for i in ids if i not in _meta]
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(message_id: str) -> None:
        async with gate:
            data = await _get(http, f"messages/{message_id}", [
                ("format", "metadata"), ("metadataHeaders", "From"),
                ("metadataHeaders", "To"), ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Message-ID")])
            _meta[message_id] = parse_metadata(data)

    await asyncio.gather(*(one(i) for i in missing))


def _lines(ids: list[str], unread_ids: set[str], account: str) -> str:
    rows = [header_line(_meta[i], account, i in unread_ids) for i in ids if i in _meta]
    return "\n".join(rows)


async def _sync_headers(http, account: str) -> None:
    from service.tools import email_tools
    ids = await _list_ids(http, HEADERS_LIMIT)
    unread = set(await _list_ids(http, HEADERS_LIMIT, unread=True))
    await _ensure_meta(http, ids)
    email_tools.cache_external_emails(headers=_lines(ids, unread, account))
    _last["headers"] = time.time()


async def _sync_raw(http, account: str) -> None:
    from service.tools import email_tools
    ids = await _list_ids(http, RAW_LIMIT)
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(message_id: str) -> dict:
        async with gate:
            return await _get(http, f"messages/{message_id}", [("format", "full")])

    messages = await asyncio.gather(*(one(i) for i in ids))
    email_tools.cache_external_emails(
        raw=_RS.join(raw_record(m, account) for m in messages))
    _last["raw"] = time.time()


async def _sync_history(http, account: str) -> None:
    from service.tools import email_tools
    ids = await _list_ids(http, HISTORY_LIMIT, query=f"newer_than:{HISTORY_DAYS}d")
    unread = set(await _list_ids(http, HISTORY_LIMIT, unread=True))
    await _ensure_meta(http, ids)
    email_tools.cache_external_emails(history=_lines(ids, unread, account))
    _last["history"] = time.time()


async def sync(parts: tuple[str, ...] = ("headers",)) -> bool:
    """Run the requested syncs; True when they all succeeded. Never raises."""
    global _lock, _last_error
    if not is_connected():
        return False
    if _lock is None:
        _lock = asyncio.Lock()
    account = load_config()["email"]
    async with _lock:
        try:
            from service.tools import email_tools
            email_tools.set_external_account(account)
            async with httpx.AsyncClient(timeout=30) as http:
                for part in parts:
                    await {"headers": _sync_headers, "raw": _sync_raw,
                           "history": _sync_history}[part](http, account)
            _last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 — a failed sync keeps the old cache
            _last_error = str(exc)[:200]
            return False


async def refresh_if_stale(max_age: float = 60.0, timeout: float = 12.0) -> None:
    """On-demand freshness for a user's mail question (see sync_status)."""
    if is_connected() and time.time() - _last["headers"] > max_age:
        try:
            await asyncio.wait_for(sync(("headers",)), timeout)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    """Background loop: headers 5 min, bodies 15 min, history 30 min."""
    while True:
        if is_connected():
            now = time.time()
            parts = ["headers"]
            if now - _last["raw"] >= RAW_EVERY:
                parts.append("raw")
            if now - _last["history"] >= HISTORY_EVERY:
                parts.append("history")
            await sync(tuple(parts))
        await asyncio.sleep(60 if not is_connected() else HEADERS_EVERY)


# --- OAuth (installed-app loopback + PKCE) -----------------------------------

async def begin_authorization(client_id: str, client_secret: str = "",
                              timeout: float = 300.0) -> str:
    """Start the loopback listener and return the Google consent URL."""
    global _pending_flow
    if _pending_flow:
        _pending_flow["server"].close()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    done = asyncio.Event()

    async def handle(reader, writer):
        global _pending_flow
        message = "Wisp could not connect Gmail."
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            query = parse_qs(urlparse(line.split(" ")[1]).query) if " " in line else {}
            if query.get("state", [""])[0] == state and query.get("code"):
                await _finish(client_id, client_secret, query["code"][0], verifier, redirect)
                message = "Gmail connected. You can close this tab and return to Wisp."
            elif query:
                message = f"Gmail was not connected ({query.get('error', ['denied'])[0]})."
            else:
                return writer.close()
        except Exception as exc:  # noqa: BLE001
            message = f"Wisp could not connect Gmail: {str(exc)[:120]}"
        body = f"<html><body style='font-family:system-ui;padding:3em'>{html.escape(message)}</body></html>".encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                     b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        done.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    redirect = f"http://127.0.0.1:{port}"
    _pending_flow = {"server": server}

    async def expire() -> None:
        global _pending_flow
        try:
            await asyncio.wait_for(done.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        server.close()
        if _pending_flow and _pending_flow["server"] is server:
            _pending_flow = None

    asyncio.create_task(expire())
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent"})


async def _finish(client_id, client_secret, code, verifier, redirect) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(TOKEN_URL, data={
            "client_id": client_id, "client_secret": client_secret, "code": code,
            "code_verifier": verifier, "redirect_uri": redirect,
            "grant_type": "authorization_code"})
        body = response.json()
        if response.status_code != 200 or not body.get("refresh_token"):
            raise GmailError(f"token exchange failed: {body.get('error', response.status_code)}")
        profile = await http.get(f"{API}/profile",
                                 headers={"Authorization": f"Bearer {body['access_token']}"})
        email = profile.json().get("emailAddress", "")
    if not email:
        raise GmailError("could not read the Gmail address")
    _meta.clear()
    _access.update(token=body["access_token"], expires=time.time() + float(body.get("expires_in", 3600)))
    _save_config({"client_id": client_id, "client_secret": client_secret,
                  "email": email, "refresh_token": body["refresh_token"]})
    asyncio.create_task(sync(("headers", "raw", "history")))


async def disconnect() -> None:
    cfg = load_config()
    token = cfg.get("refresh_token")
    if token:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post("https://oauth2.googleapis.com/revoke", data={"token": token})
        except Exception:  # noqa: BLE001 — local removal is what matters
            pass
    try:
        CONFIG_PATH.unlink()
    except FileNotFoundError:
        pass
    _meta.clear()
    _access.update(token="", expires=0.0)
    from service.tools import email_tools
    email_tools.set_external_account("")
