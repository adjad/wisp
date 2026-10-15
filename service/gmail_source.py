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
import sys
import threading
import time
import weakref
import math
from dataclasses import asdict, dataclass, field, replace
from contextvars import ContextVar
from email.header import decode_header, make_header
from email.utils import parseaddr
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from service.paths import MOE_DIR
from service.publication_authority import Authority, Witness, mutating, revocation

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
_behavior_history_sources: weakref.WeakSet = weakref.WeakSet()
_history_config_lock = threading.RLock()


def new_behavior_history(*, admission, limits, clock=time.time, monotonic=time.monotonic):
    """Explicit disabled provider core; trusted admission/transport wiring is caller-owned.

    No production caller starts this provider. It never borrows the interactive
    recent-body cache or weakens native raw_email_snapshot completeness.
    """
    from service.gmail_behavior_history import HistoryProvider
    source = HistoryProvider(admission=admission, limits=limits, clock=clock, monotonic=monotonic)
    with _history_guard():
        _behavior_history_sources.add(source)
    return source


def invalidate_behavior_history(reason="invalidated") -> None:
    """Synchronous withdrawal for consent/Forget/account-set lifecycle adapters.

    Account-set revisions bind every job, so changing even one connection
    conservatively withdraws all registered history leases before any await.
    Native Mail fallback and interactive caches follow their existing lifecycle.
    """
    with _history_guard():
        for source in list(_behavior_history_sources):
            # A later config observation must not erase the user's withdrawal
            # reason. Read only source-owned memory, never admission callbacks.
            state = getattr(getattr(source, "_provider", source), "_state", "")
            keep = state if reason == "invalidated" and state in (
                "paused", "cancelled", "revoked", "forgotten", "disconnected") else reason
            source.invalidate(keep)


# --- config / token -----------------------------------------------------------

@mutating
def load_config() -> dict:
    with _history_config_lock:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 — absent/corrupt means "not connected"
            data = {}
        _observe_history_config(data)
        return data


@mutating
def _save_config(cfg: dict) -> None:
    with _history_config_lock:
        _withdraw_history_configuration()
        MOE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle)
        tmp.replace(CONFIG_PATH)
        _observe_history_config(cfg)


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
                              timeout: float = 300.0, mail_account_id: str = "", *,
                              include_receipt: bool = False) -> str | dict:
    """Start the loopback listener and return the Google consent URL."""
    global _pending_flow, _flow_generation
    _withdraw_history_configuration()
    _flow_generation += 1
    generation = _flow_generation
    _authorization.update(state="authorizing", generation=generation, error="", email="")
    if _pending_flow:
        _pending_flow["server"].close()
        _pending_flow = None
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

    try:
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
    except Exception as exc:
        if generation == _flow_generation:
            _authorization.update(state="failed", error="Couldn’t start Google sign-in. Try again.")
            _flow_generation += 1
        raise GmailError("Couldn’t start Google sign-in. Try again.") from exc
    if generation != _flow_generation:
        server.close()
        raise GmailError("This sign-in attempt was replaced by another attempt. Try again.")
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
    url = AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent"})
    return {"auth_url": url, "generation": generation} if include_receipt else url


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
    with revocation():
        cfg = load_config()
        accounts = connected_accounts(cfg)
        if mail_account_id and any(item.get("mail_account_id") == mail_account_id and other != email
                                   for other, item in accounts.items()):
            raise GmailError("That Mail account is already linked to a different Google account")
        invalidate_behavior_history()
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
    with revocation():
        invalidate_behavior_history("disconnected")
        _withdraw_history_configuration()
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
            with _history_config_lock:
                CONFIG_PATH.unlink(missing_ok=True)
                _observe_history_config({})
        E.configure_external_accounts(accounts)
    for item in removed.values():
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                await http.post("https://oauth2.googleapis.com/revoke", data={"token": item["refresh_token"]})
        except Exception:
            pass  # Local disconnect already succeeded.


# --- Explicit public history binding (disabled unless trusted callers bind it) --


def _history_guard():
    from service.gmail_behavior_history import history_source_guard
    return history_source_guard()


_HISTORY_ISSUER = object()
_history_records = {}
_history_account_ids = {}
_history_config_digest = None
_history_registry_gate = None
_history_recovery_gates = weakref.WeakSet()
_history_http_clients = weakref.WeakSet()
_history_configuration_authority = Authority("gmail-configured-accounts")


@dataclass(frozen=True)
class HistoryAccountStatus:
    account: object  # Immutable gmail_behavior_history.AccountIdentity.
    client_revision: str
    credentials_revision: str
    configured: bool = True
    display_label: str = ""  # UI only; never an identity or permission input.

    def to_metadata(self):
        return {"schema": 1, "kind": "gmail_history_account", "value": asdict(self)}


@dataclass(frozen=True, eq=False)
class HistoryAccountLease:
    """A live source object, never a deserializable account permission DTO."""
    status: HistoryAccountStatus
    _issuer: object = field(repr=False)

    @property
    def account(self):
        return self.status.account

    def to_metadata(self):
        return self.status.to_metadata()

    def __reduce_ex__(self, protocol):
        raise TypeError("History source leases cannot be restored")


@dataclass(frozen=True)
class _HistoryConfigured:
    lease: HistoryAccountLease
    address: str = field(repr=False)
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)


@mutating
def _withdraw_history_configuration():
    """Memory withdrawal before config I/O or connection-changing awaits."""
    global _history_config_digest, _history_registry_gate
    with _history_guard():
        _history_configuration_authority.withdraw()
        _history_config_digest = None
        _history_registry_gate = None
        _history_records.clear()
        invalidate_behavior_history()


def _history_recovery_blocked():
    """Cached latch only: metadata/admission must not import or check disk."""
    module = sys.modules.get("service.config.quarantine")
    gate = module.current_gate() if module is not None else None
    return module is not None and (gate is None or gate.blocked)


def _history_cached_gate():
    module = sys.modules.get("service.config.quarantine")
    return module.current_gate() if module is not None else None


@mutating
def _observe_history_config(cfg):
    """Only explicit source config observations call this; never a getter."""
    global _history_config_digest, _history_registry_gate
    if _history_recovery_blocked():
        _withdraw_history_configuration()
        return
    from service.gmail_behavior_history import AccountIdentity
    try:
        accounts = connected_accounts(cfg)
        canonical = {}
        raw = cfg.get("accounts") if isinstance(cfg, dict) else None
        if isinstance(raw, dict) and len({str(k).strip().lower() for k in raw}) != len(raw):
            accounts = {}
        for address, item in accounts.items():
            address = address.strip().lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
                raise ValueError()
            values = tuple(item.get(k, "") for k in ("client_id", "client_secret", "refresh_token"))
            if not all(isinstance(v, str) for v in values) or not values[0] or not values[2]:
                raise ValueError()
            canonical[address] = (*values, str(item.get("mail_account_id", "")))
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).digest()
    except Exception:
        canonical = {}
        digest = b"invalid"
    with _history_guard():
        gate = _history_cached_gate()
        if _history_recovery_blocked():
            _withdraw_history_configuration()
            return
        if digest == _history_config_digest and gate is _history_registry_gate:
            return
        _history_configuration_authority.withdraw()
        invalidate_behavior_history()
        _history_records.clear()
        set_revision = secrets.token_hex(16)
        for address in list(_history_account_ids):
            if address not in canonical:
                _history_account_ids.pop(address)
        for address, (client, secret, refresh, _) in canonical.items():
            opaque_id = _history_account_ids.setdefault(address, secrets.token_hex(16))
            account = AccountIdentity("gmail", opaque_id, secrets.token_hex(16), set_revision)
            status = HistoryAccountStatus(account, secrets.token_hex(16), secrets.token_hex(16),
                                          display_label=address)
            lease = HistoryAccountLease(status, _HISTORY_ISSUER)
            _history_records[opaque_id] = _HistoryConfigured(lease, address, client, secret, refresh)
        _history_config_digest = digest
        _history_registry_gate = gate
        _history_configuration_authority.publish()


@mutating
def refresh_history_configuration():
    """Explicit trusted bootstrap read. Status/admission getters never call it."""
    _register_history_recovery()
    with _history_config_lock:
        _observe_history_config(load_config())
    return history_account_status()


def history_account_status():
    """Pure cached configured metadata; no account/token/permission inference."""
    with _history_guard():
        if (_history_recovery_blocked() or
                (_history_records and _history_registry_gate is not _history_cached_gate())):
            _withdraw_history_configuration()
        return tuple(row.lease.status for row in _history_records.values())


def history_account_lease(account_id):
    with _history_guard():
        if _history_recovery_blocked() or _history_registry_gate is not _history_cached_gate():
            _withdraw_history_configuration()
            return None
        row = _history_records.get(account_id) if isinstance(account_id, str) else None
        return row.lease if row else None


def _history_lease_current(lease):
    if _history_recovery_blocked() or _history_registry_gate is not _history_cached_gate():
        return False
    if not isinstance(lease, HistoryAccountLease) or lease._issuer is not _HISTORY_ISSUER:
        return False
    row = _history_records.get(lease.account.account_id)
    return row is not None and row.lease is lease


@dataclass(frozen=True)
class HistoryPermissionState:
    """Only a trusted zero-I/O application probe supplies these server facts.

    Public request/body/checkpoint DTOs must never be installed as this probe.
    P2 must revoke/refresh before its settings/Forget/session/role mutations or
    awaits. A probe reads a single immutable in-memory snapshot, without locks.
    """
    account_ids: tuple[str, ...]
    scope_digests: tuple[str, ...]
    session_id: str
    session_epoch: int
    role: str
    role_epoch: int
    consent_epoch: int
    global_learning: bool
    email_consent: bool
    history_start: bool
    account_scopes: tuple[tuple[str, str], ...] = ()

    def allows(self, lease, scope):
        return (type(self.account_ids) is tuple and type(self.scope_digests) is tuple
                and lease.account.account_id in self.account_ids and scope.digest in self.scope_digests
                and type(self.account_scopes) is tuple
                and ((not self.account_scopes and len(self.scope_digests) == 1)
                     or (all(type(pair) is tuple and len(pair) == 2 for pair in self.account_scopes)
                         and (lease.account.account_id, scope.digest) in self.account_scopes))
                and isinstance(self.session_id, str) and 0 < len(self.session_id) <= 256
                and all(ord(c) >= 32 for c in self.session_id)
                and self.role == "gmail_history"
                and all(type(v) is int and v >= 0 for v in
                        (self.session_epoch, self.role_epoch, self.consent_epoch))
                and self.global_learning is True and self.email_consent is True and self.history_start is True)


@dataclass(frozen=True, eq=False)
class HistoryGrant:
    """Source-issued application+account lease, not request authority."""
    account_lease: HistoryAccountLease
    scope: object
    permission: HistoryPermissionState
    _application: object = field(repr=False)
    _issuer: object = field(repr=False)

    def to_metadata(self):
        return {"schema": 1, "kind": "gmail_history_grant_metadata",
                "account": self.account_lease.to_metadata(), "scope_digest": self.scope.digest,
                "session_epoch": self.permission.session_epoch, "role_epoch": self.permission.role_epoch,
                "consent_epoch": self.permission.consent_epoch}

    def __reduce_ex__(self, protocol):
        raise TypeError("History grants cannot be restored")


class HistoryApplicationAuthority:
    def __init__(self, *, probe, _issuer=None):
        if _issuer is not _HISTORY_ISSUER or not callable(probe):
            raise ValueError("Trusted application bootstrap probe required")
        self._probe = probe
        self._state = None
        self._revoked = False
        self._grants = weakref.WeakSet()
        self._bridges = weakref.WeakSet()
        self._publication = Authority("gmail-history-application", owner=self)

    def __reduce_ex__(self, protocol):
        raise TypeError("History application authority cannot be restored")

    def refresh(self):
        """Publish current trusted memory facts; withdraw before caller awaits."""
        with _history_guard():
            if self._revoked:
                return None
            try:
                state = self._probe()
                if not isinstance(state, HistoryPermissionState):
                    state = None
            except Exception:
                state = None
            if state != self._state:
                self._publication.withdraw()
                self._grants.clear()
                for bridge in list(self._bridges):
                    bridge.invalidate("revoked")
                self._state = state
                if state is not None:
                    self._publication.publish()
            return state

    def revoke(self):
        """Terminal installation withdrawal; reinstall via trusted bootstrap."""
        with _history_guard():
            self._publication.withdraw()
            self._revoked = True
            self._state = None
            self._grants.clear()
            for bridge in list(self._bridges):
                bridge.invalidate("revoked")

    def issue(self, account_lease, scope):
        from service.gmail_behavior_history import HistoryScope
        with _history_guard():
            state = self.refresh()
            if (state is None or not _history_lease_current(account_lease)
                    or not isinstance(scope, HistoryScope) or not state.allows(account_lease, scope)):
                raise PermissionError("Current configured account and application history permission required")
            grant = HistoryGrant(account_lease, scope, state, self, _HISTORY_ISSUER)
            self._grants.add(grant)
            return grant


def install_history_application(*, probe):
    """Trusted application-code bootstrap only, never route-supplied data."""
    return HistoryApplicationAuthority(probe=probe, _issuer=_HISTORY_ISSUER)


def _history_grant_current(grant):
    if not isinstance(grant, HistoryGrant) or grant._issuer is not _HISTORY_ISSUER:
        return False
    app = grant._application
    state = app.refresh()
    return (grant in app._grants and state == grant.permission
            and _history_lease_current(grant.account_lease) and state is not None
            and state.allows(grant.account_lease, grant.scope))


@mutating
def _register_history_recovery():
    from service.config import quarantine
    gate = quarantine.current_gate()
    with _history_guard():
        if gate not in _history_recovery_gates:
            gate.callbacks.append(_withdraw_history_configuration)
            _history_recovery_gates.add(gate)
    return gate


class HistoryHTTPClient:
    """Explicit source-owned fixed-policy client, no default construction.

    synthetic() uses only MockTransport. network() is an explicit future runtime
    factory, never invoked automatically. Both disable proxy environment,
    redirects and retries. Behavior never supplies token or OAuth helpers.
    """
    def __init__(self, *, transport, _issuer=None):
        if _issuer is not _HISTORY_ISSUER:
            raise ValueError("Use the explicit source HTTP factories")
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                        trust_env=False, timeout=10)
        self._gate = _register_history_recovery()
        with _history_guard():
            _history_http_clients.add(self)

    @classmethod
    def synthetic(cls, handler):
        if not callable(handler):
            raise ValueError("Synthetic HTTP handler required")
        return cls(transport=httpx.MockTransport(handler), _issuer=_HISTORY_ISSUER)

    @classmethod
    def network(cls):
        return cls(transport=httpx.AsyncHTTPTransport(retries=0), _issuer=_HISTORY_ISSUER)

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def _request(self, check, method, url, **kwargs):
        from service.config import quarantine
        # Recovery I/O is explicit transport work OUTSIDE the history guard.
        # No source/admission/getter callback checks disk or acquires its lock.
        if quarantine.current_gate() is not self._gate:
            _withdraw_history_configuration()
            raise GmailError("History credential authority changed")
        refresh_history_configuration()
        self._gate.check()
        check()
        response = await self._client.request(method, url, **{**kwargs, "follow_redirects": False})
        refresh_history_configuration()
        self._gate.check()
        check()
        return response


class _BoundHistoryHTTP:
    def __init__(self, bridge, grant, http):
        self.bridge, self.grant, self.http = bridge, grant, http

    async def request(self, method, url, **kwargs):
        response = await self.http._request(lambda: self.bridge._require_grant(self.grant), method, url, **kwargs)
        if response.status_code in (401, 403):
            _withdraw_history_configuration()
            self.bridge._binding_error = "Configured Gmail authorization could not be verified"
        return response


class PublicContinuation:
    """Opaque, volatile traversal handoff. Metadata never restores this object."""
    __slots__ = ("__weakref__",)

    def __init__(self, *, _issuer=None):
        if _issuer is not _HISTORY_ISSUER:
            raise TypeError("History continuation must be issued by its live source")

    def __copy__(self):
        raise TypeError("History continuation cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("History continuation cannot be copied")

    def __reduce_ex__(self, protocol):
        raise TypeError("History continuation cannot be restored")


class PublicHistoryBridge:
    """Prepared production binding; no production caller or default activation.

    All currentness/cache operations use the SAME core guard. Application probes
    read immutable memory without taking any other lock. Explicit config I/O uses
    application barrier -> config mutex -> history guard for withdrawal/publication,
    never the reverse. No application lease spans a provider await.
    """
    def __init__(self, *, application, limits, clock=time.time, monotonic=time.monotonic):
        from service.gmail_behavior_history import HistoryProvider
        if not isinstance(application, HistoryApplicationAuthority):
            raise ValueError("Source-installed application authority required")
        self._application = application
        self._clock = clock
        self._grant = None
        self._token = None
        self._binding_error = ""
        self._continuation = None
        self._start_identity = None
        self._provider = HistoryProvider(admission=self._admission, limits=limits, clock=clock, monotonic=monotonic)
        with _history_guard():
            application._bridges.add(self)
            _behavior_history_sources.add(self)

    def _current(self, grant):
        current = _history_grant_current(grant)
        return self._grant is grant and current

    def _require_grant(self, grant):
        from service.gmail_behavior_history import _Withdrawn
        with _history_guard():
            if not self._current(grant):
                if self._grant is grant:
                    self.invalidate("revoked")
                raise _Withdrawn()

    def _admission(self):
        from service.gmail_behavior_history import Admission
        grant = self._grant
        if grant is None or not self._current(grant):
            return None
        state = grant.permission
        return Admission(grant.account_lease.account, state.consent_epoch, state.session_epoch,
                         True, True, True, grant.scope.digest)

    def start(self, grant):
        with _history_guard():
            if (not isinstance(grant, HistoryGrant) or grant._application is not self._application
                    or not _history_grant_current(grant)):
                raise PermissionError("Live source-issued account/application history grant required")
            self.invalidate()
            # invalidate may discard a grant reused by this same bridge.
            if grant not in self._application._grants:
                raise PermissionError("Withdrawn history grant cannot restart")
            self._grant = grant
            self._binding_error = ""
            self._provider.control("start", scope=grant.scope)
            self._start_identity = object()
            return self.status()

    def invalidate(self, reason="invalidated"):
        with _history_guard():
            self._provider.invalidate(reason)
            if self._grant is not None:
                self._application._grants.discard(self._grant)
            self._grant = self._token = None
            self._continuation = self._start_identity = None

    def prepare_continuation(self):
        """Issue one live handoff before expiry; no HTTP, disk I/O or await."""
        with _history_guard():
            self.status()
            state = self._provider._continuation_status()
            if not state["available"]:
                if state["blocked_reason"]:
                    self._continuation = None
                    self._provider._stop_continuation("History continuation stopped; coverage is partial")
                raise PermissionError("Current progressing history handoff window required")
            if self._continuation is None:
                self._continuation = (PublicContinuation(_issuer=_HISTORY_ISSUER),
                                      self._provider._job, self._grant, self._start_identity)
            return self._continuation[0]

    def continue_scan(self, continuation, *, grant):
        """Consume a source-issued traversal with a NEW current unused grant.

        Same explicit Start, source lease, scope, application and immutable
        permission are mandatory. Old cache/receipt/token are withdrawn. Only
        a subsequent explicit, counted step can refresh/profile/fetch again.
        """
        with _history_guard():
            self.status()
            issued = self._continuation
            old = self._grant
            if (issued is None or continuation is not issued[0]
                    or self._provider._job is not issued[1] or old is not issued[2]
                    or self._start_identity is not issued[3]
                    or not self._provider._continuation_status()["available"]
                    or not self._current(old)
                    or not isinstance(grant, HistoryGrant) or grant is old
                    or grant._application is not self._application
                    or grant.account_lease is not old.account_lease or grant.scope != old.scope
                    or grant.permission != old.permission or not _history_grant_current(grant)
                    or any(bridge._grant is grant for bridge in self._application._bridges)):
                raise PermissionError("Live source continuation and fresh same-Start grant required")
            # No I/O/await between final old/new validation and atomic transfer.
            self._continuation = None
            self._application._grants.discard(old)
            self._grant, self._token = grant, None
            try:
                self._provider._continue_segment()
                self._require_grant(grant)
            except BaseException:
                self.invalidate("revoked")
                raise
            return self.status()

    def control(self, action):
        actions = {"pause": "paused", "cancel": "cancelled", "revoke": "revoked", "forget": "forgotten"}
        if action not in actions:
            raise ValueError("Start requires a live source-issued grant")
        self.invalidate(actions[action])
        return self.status()

    def status(self):
        with _history_guard():
            grant = self._grant
            if grant is not None and not self._current(grant):
                self.invalidate("revoked")
            status = self._provider.status()
            if self._grant is not None and status["state"] not in ("running", "exhausted", "capped", "partial"):
                self.invalidate(status["state"])
                status = self._provider.status()
            return {**status, "binding": self._grant.to_metadata() if self._grant else None,
                    "binding_error": self._binding_error,
                    "continuation": self._provider._continuation_status()}

    def snapshot(self, *, expected_receipt=None, required_refs=()):
        with _history_guard():
            self.status()
            snapshot = self._provider.snapshot(expected_receipt=expected_receipt, required_refs=required_refs)
            if snapshot is None:
                return None
            from service.config import quarantine
            witness = Witness.combine(snapshot.cache_witness,
                _history_configuration_authority.token(), self._application._publication.token(),
                quarantine.publication_token(_history_registry_gate))
            return replace(snapshot, publication_witness=witness)

    def checkpoint(self):
        with _history_guard():
            # Traversal stays source-owned; this export is never resume authority.
            return {"schema": 1, "status": self.status()}

    async def _source_token(self, http):
        from service import gmail_behavior_history as H
        grant = self._grant
        self._require_grant(grant)
        cached = self._token
        if cached and cached[0] is grant and self._clock() < cached[2] - 60:
            return H.HistoryAccessToken(http.admission, cached[1])
        with _history_guard():
            self._require_grant(grant)
            record = _history_records[grant.account_lease.account.account_id]
        try:
            response = await http.post(TOKEN_URL, data={"client_id": record.client_id,
                "client_secret": record.client_secret, "refresh_token": record.refresh_token,
                "grant_type": "refresh_token"})
            self._require_grant(grant)
            data = response.json()
            if (response.status_code != 200 or not isinstance(data, dict)
                    or not isinstance(data.get("access_token"), str) or not data["access_token"]
                    or SCOPE not in str(data.get("scope", "")).split()
                    or data.get("token_type", "").lower() != "bearer"):
                raise GmailError("History read authorization unavailable")
            expires = data.get("expires_in")
            if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                    or not math.isfinite(expires) or not 60 < expires <= 86400):
                raise GmailError("History token expiry unavailable")
            profile = await http.get(API + "/profile", headers={"Authorization": f"Bearer {data['access_token']}"})
            self._require_grant(grant)
            owner = profile.json()
            if (profile.status_code != 200 or not isinstance(owner, dict)
                    or not isinstance(owner.get("emailAddress"), str)
                    or owner["emailAddress"].strip().lower() != record.address):
                raise GmailError("History token does not match configured account")
            with _history_guard():
                self._require_grant(grant)
                self._token = (grant, data["access_token"], self._clock() + expires)
            return H.HistoryAccessToken(http.admission, data["access_token"])
        except (H._BudgetStop, H._Withdrawn):
            raise
        except Exception:
            with _history_guard():
                if self._grant is grant:
                    self.invalidate("revoked")
                    self._binding_error = "Configured Gmail authorization could not be verified"
            raise GmailError("Configured Gmail authorization could not be verified") from None

    async def step(self, *, http, budget):
        from service.gmail_behavior_history import StepBudget
        with _history_guard():
            if type(http) is not HistoryHTTPClient or http not in _history_http_clients:
                raise ValueError("Explicit source-owned history HTTP client required")
            if not isinstance(budget, StepBudget):
                raise ValueError("Explicit bounded history step budget required")
            self.status()
            grant = self._grant
            if grant is None:
                return self.status()
        try:
            await self._provider.step(http=_BoundHistoryHTTP(self, grant, http), token=self._source_token, budget=budget)
        except asyncio.CancelledError:
            with _history_guard():
                if self._grant is grant:
                    self.invalidate("cancelled")
            raise
        return self.status()


def new_public_history_bridge(*, application, limits, clock=time.time, monotonic=time.monotonic):
    return PublicHistoryBridge(application=application, limits=limits, clock=clock, monotonic=monotonic)
