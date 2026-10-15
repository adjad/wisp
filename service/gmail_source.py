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
from contextvars import ContextVar
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
HISTORY_EVERY = 1800

_FS = "\x01"
_RS = "\x02"

_meta: dict[tuple[str, str], dict] = {}
_last: dict[str, dict[str, float]] = {}
_errors: dict[str, str] = {}
_access: dict[str, dict] = {}
_epochs: dict[str, int] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}
_context: ContextVar[tuple[str, dict, int] | None] = ContextVar("gmail_account", default=None)
_pending_flow: dict | None = None
_flow_generation = 0
_authorization: dict = {"state": "idle", "generation": 0, "error": ""}


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
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle)
    tmp.replace(CONFIG_PATH)


def connected_accounts(cfg: dict | None = None) -> dict[str, dict]:
    cfg = load_config() if cfg is None else cfg
    accounts = cfg.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {str(cfg.get("email") or "").lower(): cfg} if cfg.get("email") else {}
    return {email.lower(): {"client_id": cfg.get("client_id", ""),
                            "client_secret": cfg.get("client_secret", ""), **item}
            for email, item in accounts.items() if isinstance(item, dict) and
            item.get("refresh_token") and (item.get("client_id") or cfg.get("client_id"))}


def is_connected() -> bool:
    return bool(connected_accounts())


def status() -> dict:
    from service.tools import email_tools as E
    cfg = load_config()
    accounts = connected_accounts(cfg)
    sources = {row["account"]: row for row in E.email_source_details() if row["source"] == "gmail"}
    rows = [{"email": email, "last_headers_sync": sources.get(email, {}).get("last_success"),
             "fresh": sources.get(email, {}).get("fresh", False),
             "last_error": _errors.get(email) or sources.get(email, {}).get("error", ""),
             "mail_account_id": item.get("mail_account_id", "")}
            for email, item in accounts.items()]
    first = rows[0] if rows else {}
    return {"connected": bool(rows), "email": first.get("email", ""), "accounts": rows,
            "client_id": cfg.get("client_id", ""), "has_secret": bool(cfg.get("client_secret")),
            "last_headers_sync": first.get("last_headers_sync"), "last_error": first.get("last_error", ""),
            "authorizing": _pending_flow is not None, "authorization": dict(_authorization),
            "mail_accounts": E.native_email_accounts()}


def _current() -> tuple[str, dict, int]:
    value = _context.get()
    if value is None:
        raise GmailError("No Gmail account selected")
    return value


def _still_connected() -> bool:
    email, cfg, epoch = _current()
    actual = connected_accounts().get(email)
    return bool(actual and actual.get("refresh_token") == cfg.get("refresh_token") and
                _epochs.get(email, 0) == epoch)


async def _access_token(http: httpx.AsyncClient) -> str:
    email, cfg, _ = _current()
    if not _still_connected():
        raise GmailError("Gmail connection changed during refresh")
    cached = _access.get(email, {})
    if cached.get("token") and time.time() < cached.get("expires", 0) - 60:
        return cached["token"]
    if not _still_connected():
        raise GmailError("Gmail connection changed during refresh")
    response = await http.post(TOKEN_URL, data={
        "client_id": cfg.get("client_id", ""), "client_secret": cfg.get("client_secret", ""),
        "refresh_token": cfg.get("refresh_token", ""), "grant_type": "refresh_token"})
    body = response.json() if response.content else {}
    if response.status_code != 200 or "access_token" not in body:
        raise GmailError("Google sign-in expired or was revoked; reconnect this account in Settings ▸ Google"
                         if body.get("error") == "invalid_grant" else "Google token refresh failed")
    if body.get("scope") and SCOPE not in str(body["scope"]).split():
        raise GmailError("Gmail read access is missing; reconnect this account in Settings ▸ Google")
    if not _still_connected():
        raise GmailError("Gmail connection changed during refresh")
    _access[email] = {"token": body["access_token"], "expires": time.time() + float(body.get("expires_in", 3600))}
    return body["access_token"]


class GmailError(RuntimeError):
    pass


async def _get(http: httpx.AsyncClient, path: str, params: dict | list | None = None) -> dict:
    for attempt in range(4):
        if not _still_connected():
            raise GmailError("Gmail connection changed during refresh")
        token = await _access_token(http)
        if not _still_connected():
            raise GmailError("Gmail connection changed during refresh")
        response = await http.get(f"{API}/{path}", params=params,
                                  headers={"Authorization": f"Bearer {token}"})
        if not _still_connected():
            raise GmailError("Gmail connection changed during refresh")
        if response.status_code == 401:
            _access.pop(_current()[0], None)
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
    account = _current()[0]
    missing = [i for i in ids if (account, i) not in _meta]
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(message_id: str) -> None:
        async with gate:
            data = await _get(http, f"messages/{message_id}", [
                ("format", "metadata"), ("metadataHeaders", "From"),
                ("metadataHeaders", "To"), ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Message-ID")])
            _meta[(account, message_id)] = parse_metadata(data)

    await asyncio.gather(*(one(i) for i in missing))


def _lines(ids: list[str], unread_ids: set[str], account: str) -> str:
    rows = [header_line(_meta[(account, i)], account, i in unread_ids)
            for i in ids if (account, i) in _meta]
    return "\n".join(rows)


async def _sync_headers(http, account: str) -> None:
    from service.tools import email_tools
    ids = await _list_ids(http, HEADERS_LIMIT)
    unread = set(await _list_ids(http, HEADERS_LIMIT, unread=True))
    await _ensure_meta(http, ids)
    if not _still_connected():
        return
    cap = int(len(ids) >= HEADERS_LIMIT)
    headers = _lines(ids, unread, account) + f"\nC2\x01{account}\x01{account}\x01{len(ids)}\x010\x01{cap}"
    email_tools.cache_external_emails(account=account, headers=headers)
    _last.setdefault(account, {})["headers"] = time.time()


async def _sync_raw(http, account: str) -> None:
    from service.tools import email_tools
    ids = await _list_ids(http, RAW_LIMIT)
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(message_id: str) -> dict:
        async with gate:
            return await _get(http, f"messages/{message_id}", [("format", "full")])

    messages = await asyncio.gather(*(one(i) for i in ids))
    if not _still_connected():
        return
    email_tools.cache_external_emails(account=account,
        raw=_RS.join(raw_record(m, account) for m in messages))
    _last.setdefault(account, {})["raw"] = time.time()


async def _sync_history(http, account: str) -> None:
    from service.tools import email_tools
    ids = await _list_ids(http, HISTORY_LIMIT, query=f"newer_than:{HISTORY_DAYS}d")
    unread = set(await _list_ids(http, HISTORY_LIMIT, unread=True))
    await _ensure_meta(http, ids)
    if not _still_connected():
        return
    cap = int(len(ids) >= HISTORY_LIMIT)
    history = _lines(ids, unread, account) + f"\nC2\x01{account}\x01{account}\x01{len(ids)}\x010\x01{cap}"
    email_tools.cache_external_emails(account=account, history=history)
    _last.setdefault(account, {})["history"] = time.time()


async def _sync_account(account: str, parts: tuple[str, ...]) -> bool:
    from service.tools import email_tools as E
    # Capture config INSIDE the per-account lock. Disconnect/reconnect increments
    # an epoch and invalidates every in-flight publication and token write.
    if len(parts) > 1:
        return all(await asyncio.gather(*(_sync_account(account, (part,)) for part in parts)))
    lock = _locks.setdefault((account, parts[0]), asyncio.Lock())
    async with lock:
        cfg = connected_accounts().get(account)
        if cfg is None:
            return False
        token = _context.set((account, dict(cfg), _epochs.get(account, 0)))
        part = parts[0]
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                for part in parts:
                    await {"headers": _sync_headers, "raw": _sync_raw,
                           "history": _sync_history}[part](http, account)
            if not _still_connected():
                return False
            if part == "headers":
                _errors.pop(account, None)
            return True
        except asyncio.CancelledError:
            if _still_connected():
                reason = "Google refresh timed out; showing the last available cache"
                if part == "headers":
                    _errors[account] = reason
                E.external_email_error(account, part, reason)
            raise
        except Exception as exc:
            if _still_connected():
                # Never expose HTTP payloads, query strings, tokens, or PII in errors.
                reason = str(exc) if isinstance(exc, GmailError) else "Google could not be reached; try again when online"
                if part == "headers":
                    _errors[account] = reason
                E.external_email_error(account, part, reason)
            return False
        finally:
            _context.reset(token)


async def sync(parts: tuple[str, ...] = ("headers",)) -> bool:
    from service.tools import email_tools as E
    accounts = connected_accounts()
    E.configure_external_accounts(accounts)
    if not accounts:
        return False
    results = await asyncio.gather(*(_sync_account(account, parts) for account in accounts))
    return all(results)


async def refresh_if_stale(max_age: float = 60.0, timeout: float = 12.0,
                           parts: tuple[str, ...] = ("headers",)) -> None:
    """Bounded on-demand refresh. Checking headers never fetches bodies."""
    from service.tools import email_tools as E
    accounts = connected_accounts()
    if not accounts:
        return
    E.configure_external_accounts(accounts)

    async def refresh(account):
        needed = tuple(part for part in parts if not _last.get(account, {}).get(part) or
                       not 0 <= time.time() - _last[account][part] <= max_age)
        if not needed:
            return
        try:
            await asyncio.wait_for(_sync_account(account, needed), timeout)
        except asyncio.TimeoutError:
            for part in needed:
                E.external_email_error(account, part, "Google refresh timed out; showing the last available cache")

    await asyncio.gather(*(refresh(account) for account in accounts))


async def run() -> None:
    """Fetch providers independently; a slow history walk cannot block new mail."""
    from service.tools import email_tools as E
    E.configure_external_accounts(connected_accounts())
    while True:
        accounts = connected_accounts()
        E.configure_external_accounts(accounts)
        async def update(account, part):
            try:
                await asyncio.wait_for(_sync_account(account, (part,)), 30)
            except asyncio.TimeoutError:
                pass
        now = time.time()
        await asyncio.gather(*(update(account, part) for account in accounts
                               for part, every in (("headers", HEADERS_EVERY), ("history", HISTORY_EVERY))
                               if now - _last.get(account, {}).get(part, 0) >= every))
        # Body content is fetched only for explicit body requests, not in the background.
        await asyncio.sleep(60)


# --- OAuth (installed-app loopback + PKCE) -----------------------------------

async def begin_authorization(client_id: str, client_secret: str = "",
                              timeout: float = 300.0, mail_account_id: str = "") -> str:
    """Start the loopback listener and return the Google consent URL."""
    global _pending_flow, _flow_generation
    _flow_generation += 1
    generation = _flow_generation
    _authorization.update(state="authorizing", generation=generation, error="", email="")
    if _pending_flow:
        _pending_flow["server"].close()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    done = asyncio.Event()

    async def handle(reader, writer):
        global _pending_flow, _flow_generation
        message = "Wisp could not connect Gmail."
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            query = parse_qs(urlparse(line.split(" ")[1]).query) if " " in line else {}
            if query.get("state", [""])[0] != state or generation != _flow_generation:
                return writer.close()  # Unrelated/stale callbacks cannot consume the active flow.
            if query.get("code"):
                email = await _finish(client_id, client_secret, query["code"][0], verifier, redirect,
                                      generation=generation, mail_account_id=mail_account_id)
                if generation != _flow_generation:
                    return writer.close()
                _authorization.update(state="connected", email=email, error="")
                _flow_generation += 1
                message = "Gmail connected. You can close this tab and return to Wisp."
            elif query:
                message = "Google sign-in was declined or cancelled. Your existing accounts were kept."
                _authorization.update(state="failed", error=message)
                _flow_generation += 1
            else:
                return writer.close()
        except Exception as exc:  # noqa: BLE001
            message = f"Wisp could not connect Gmail: {str(exc)[:120]}"
            if generation == _flow_generation:
                _authorization.update(state="failed", error=message)
                _flow_generation += 1
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
        global _pending_flow, _flow_generation
        try:
            await asyncio.wait_for(done.wait(), timeout)
        except asyncio.TimeoutError:
            if generation == _flow_generation:
                _authorization.update(state="failed", error="Google sign-in timed out. Try again.")
                _flow_generation += 1
        server.close()
        if _pending_flow and _pending_flow["server"] is server:
            _pending_flow = None

    asyncio.create_task(expire())
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent"})


async def _finish(client_id, client_secret, code, verifier, redirect, *,
                  generation: int, mail_account_id: str = "") -> str | None:
    if generation != _flow_generation:
        return None
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(TOKEN_URL, data={
            "client_id": client_id, "client_secret": client_secret, "code": code,
            "code_verifier": verifier, "redirect_uri": redirect,
            "grant_type": "authorization_code"})
        if generation != _flow_generation:
            return None
        body = response.json()
        if response.status_code != 200 or not body.get("refresh_token"):
            why = body.get("error_description") or body.get("error") or response.status_code
            hint = (" — paste your OAuth client secret in Settings ▸ Google and try again"
                    if "secret" in str(why).lower() or body.get("error") == "invalid_request"
                    and not client_secret else "")
            raise GmailError(f"token exchange failed: {why}{hint}")
        if SCOPE not in str(body.get("scope", "")).split():
            raise GmailError("Gmail read access wasn't granted — on Google's consent screen, "
                             "tick the box to view your email messages, then try again")
        profile = await http.get(f"{API}/profile",
                                 headers={"Authorization": f"Bearer {body['access_token']}"})
        data = profile.json() if profile.content else {}
        email = data.get("emailAddress", "")
    if profile.status_code != 200 or not email:
        detail = (data.get("error") or {}) if isinstance(data.get("error"), dict) else {}
        reason = detail.get("message") or f"HTTP {profile.status_code}"
        raise GmailError(f"Google refused the Gmail profile request: {reason[:160]}")
    if generation != _flow_generation:
        return
    email = email.strip().lower()
    cfg = load_config()
    accounts = connected_accounts(cfg)
    if mail_account_id and any(item.get("mail_account_id") == mail_account_id and other != email
                               for other, item in accounts.items()):
        raise GmailError("That Mail account is already linked to a different Google account")
    _epochs[email] = _epochs.get(email, 0) + 1
    _access.pop(email, None)
    _last.pop(email, None)
    accounts[email] = {"client_id": client_id, "client_secret": client_secret,
                       "refresh_token": body["refresh_token"], "mail_account_id": mail_account_id}
    _save_config({"client_id": client_id, "client_secret": client_secret, "accounts": accounts})
    from service.tools import email_tools as E
    E.configure_external_accounts(accounts)
    E.external_email_error(email, "headers", "New sign-in is waiting for its first refresh")
    # Header-only first connection; bodies remain on demand.
    asyncio.create_task(sync(("headers",)))
    return email


async def disconnect(account: str = "") -> None:
    """Invalidate before awaiting revocation; a late fetch cannot restore access."""
    global _pending_flow, _flow_generation
    from service.tools import email_tools as E
    _flow_generation += 1
    _authorization.update(state="cancelled", generation=_flow_generation, error="Sign-in cancelled by disconnect", email="")
    if _pending_flow:
        _pending_flow["server"].close()
        _pending_flow = None
    cfg = load_config()
    accounts = connected_accounts(cfg)
    removed = {email: item for email, item in accounts.items() if not account or email == account.lower()}
    for email in removed:
        accounts.pop(email)
        _epochs[email] = _epochs.get(email, 0) + 1
        _access.pop(email, None)
        _last.pop(email, None)
        _errors.pop(email, None)
        for key in list(_meta):
            if key[0] == email:
                _meta.pop(key, None)
    if accounts:
        _save_config({"client_id": cfg.get("client_id", ""), "client_secret": cfg.get("client_secret", ""),
                      "accounts": accounts})
    else:
        CONFIG_PATH.unlink(missing_ok=True)
    E.configure_external_accounts(accounts)
    for item in removed.values():
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                await http.post("https://oauth2.googleapis.com/revoke", data={"token": item["refresh_token"]})
        except Exception:
            pass  # Local disconnect already succeeded.
