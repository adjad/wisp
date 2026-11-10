"""Ask the app for Mail data, one request at a time (MailMemoryReader.swift answers).

Mail memory asks for `index` and `body`; Ask's sent-mail read (view_emails
mailbox='sent') asks for `sent`: the user's own sent mail, headers only.

The backend can't read Mail's files: Full Disk Access belongs to the app. So a
request is a non-durable `mail_memory_read` hub event carrying a random,
single-use `request_id`, and the app answers by POST to
/assistant/sync/mail_memory. A reply is accepted only for a request still
waiting here, of the kind that was asked, so nothing can push mail in unasked.
"""
from __future__ import annotations

import asyncio
import base64
import math
import re
import secrets

_pending: dict[str, tuple[str, asyncio.Future]] = {}


class BridgeError(RuntimeError):
    pass


async def _ask(kind: str, timeout: float, **fields) -> dict:
    from service.assistant.hub import hub
    if not hub.has_subscribers:
        raise BridgeError("app_not_connected")
    request_id = secrets.token_urlsafe(24)
    future = asyncio.get_running_loop().create_future()
    _pending[request_id] = (kind, future)
    try:
        # Not durable: a read the app missed is asked again next tick, never replayed later.
        await hub.publish({"type": "mail_memory_read", "request_id": request_id, "kind": kind, **fields},
                          durable=False)
        reply = await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        raise BridgeError(f"{kind}_timeout") from None
    finally:
        _pending.pop(request_id, None)
    return reply


def deliver(reply: dict) -> bool:
    """Hand an app reply to the request waiting for it. False = unknown or wrong kind."""
    request_id = reply.get("request_id")
    if not isinstance(request_id, str):
        return False
    waiting = _pending.get(request_id)
    if waiting is None or waiting[0] != reply.get("kind") or waiting[1].done():
        return False
    waiting[1].set_result(reply)
    return True


def _category(value) -> int:
    """Mail's category (0 Primary, 1 Transactions, 2 Updates, 3 Promotions), -1 unknown."""
    return value if type(value) is int and 0 <= value <= 3 else -1


def _rows(reply: dict) -> list[dict]:
    out = []
    for r in reply.get("rows") or []:
        if not isinstance(r, dict):
            continue
        try:
            rowid = int(r["rowid"])
            ts = float(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if rowid <= 0 or r.get("box") not in ("inbox", "sent"):
            continue
        out.append({"rowid": rowid, "account": str(r.get("account") or "")[:80], "box": r["box"], "ts": ts,
                    "sent_ts": float(r.get("sent_ts") or 0), "message_id": str(r.get("message_id") or "")[:300],
                    "conversation_id": int(r.get("conversation_id") or 0),
                    "subject": str(r.get("subject") or "")[:300], "sender": str(r.get("sender") or "")[:200],
                    "sender_name": str(r.get("sender_name") or "")[:200], "bulk": r.get("bulk") is True,
                    "category": _category(r.get("category")),
                    "rfc_message_id": str(r.get("rfc_message_id") or "")[:300]})
    return out


async def read_index(days: int, timeout: float = 60.0) -> tuple[list[dict], bool]:
    reply = await _ask("index", timeout, days=int(days))
    if reply.get("ok") is not True:
        raise BridgeError(str(reply.get("error") or "index_failed")[:40])
    return _rows(reply), reply.get("capped") is True


async def read_body(rowid: int, timeout: float = 15.0) -> tuple[bytes | None, str]:
    """The message's raw bytes, or (None, why). `not_on_disk` is final for that message."""
    reply = await _ask("body", timeout, rowid=int(rowid))
    if reply.get("ok") is not True:
        return None, str(reply.get("error") or "body_failed")[:40]
    try:
        return base64.b64decode(str(reply.get("raw_b64") or ""), validate=True), ""
    except ValueError:
        return None, "bad_base64"


# The app answers `sent` for at most this many days back (MailMemoryReader.maxSentDays).
SENT_DAYS_MAX = 31


def _sent_rows(reply: dict) -> list[dict]:
    out = []
    for r in reply.get("rows") or []:
        if not isinstance(r, dict):
            continue
        try:
            ts = float(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        to = r.get("to") if isinstance(r.get("to"), list) else []
        total = r.get("recipients")
        out.append({"ts": ts, "account": str(r.get("account") or "")[:80],
                    "subject": str(r.get("subject") or "")[:300],
                    "rfc_message_id": str(r.get("rfc_message_id") or "")[:300],
                    "to": [str(x)[:200] for x in to[:5] if isinstance(x, str) and x],
                    "recipients": total if type(total) is int and total >= 0 else 0})
    return out


async def read_sent(days: int, timeout: float = 8.0) -> tuple[list[dict], bool]:
    """Apple Mail's sent mail for the last `days` (at most SENT_DAYS_MAX), newest
    first, headers and recipients only, and whether the app capped the list."""
    reply = await _ask("sent", timeout, days=max(1, min(int(days), SENT_DAYS_MAX)))
    if reply.get("ok") is not True:
        raise BridgeError(str(reply.get("error") or "sent_failed")[:40])
    return _sent_rows(reply), reply.get("capped") is True


# --- attachments (MailMemoryReader kinds `attachments` and `attachment`) -----------

_ATTACHMENT_ID = re.compile(r"^[0-9A-Za-z._-]{1,40}$")
ATTACHMENT_TERMS_MAX = 5
ATTACHMENT_RFC_IDS_MAX = 40
ATTACHMENT_LIMIT_MAX = 400


def norm_rfc(value) -> str:
    """An RFC Message-ID without brackets, casefolded: the key both sources share."""
    return str(value or "").strip().strip("<>").strip().casefold()


def _attachment_rows(reply: dict) -> list[dict]:
    out = []
    for r in reply.get("rows") or []:
        if not isinstance(r, dict):
            continue
        try:
            rowid = int(r["rowid"])
            ts = float(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        attachment_id = str(r.get("attachment_id") or "")
        name = str(r.get("name") or "")[:255]
        if rowid <= 0 or not _ATTACHMENT_ID.match(attachment_id) or not name:
            continue
        size = r.get("size")
        out.append({"rowid": rowid, "ts": ts, "account": str(r.get("account") or "")[:80],
                    "box": "sent" if r.get("box") == "sent" else "other",
                    "subject": str(r.get("subject") or "")[:300], "sender": str(r.get("sender") or "")[:200],
                    "sender_name": str(r.get("sender_name") or "")[:200],
                    "rfc_message_id": str(r.get("rfc_message_id") or "")[:300],
                    "attachment_id": attachment_id, "name": name, "on_disk": r.get("on_disk") is True,
                    "size": size if type(size) is int and size >= 0 else 0})
    return out


async def read_attachment_index(terms: list[str], since: float, until: float, rfc_ids: list[str],
                                limit: int = 300, timeout: float = 10.0) -> tuple[list[dict], bool]:
    """Apple Mail's attachments, one row per file, newest first, and whether the app capped
    the list. Every term must match subject, sender or file name; `rfc_ids` (when given)
    restricts it to those messages. Raises BridgeError."""
    cleaned = [str(t).strip()[:80] for t in terms or [] if str(t).strip()][:ATTACHMENT_TERMS_MAX]
    bare = list(dict.fromkeys(b for b in (norm_rfc(i) for i in rfc_ids or []) if b))
    # Both forms go to the app: Mail stores some Message-IDs with brackets and some without.
    both = [form for b in bare[:ATTACHMENT_RFC_IDS_MAX // 2] for form in (b, f"<{b}>")]
    reply = await _ask("attachments", timeout, terms=cleaned, since=float(since or 0), until=float(until or 0),
                       rfc_ids=both, limit=max(1, min(int(limit), ATTACHMENT_LIMIT_MAX)))
    if reply.get("ok") is not True:
        raise BridgeError(str(reply.get("error") or "attachments_failed")[:40])
    return _attachment_rows(reply), reply.get("capped") is True


async def read_attachment(rowid: int, attachment_id: str, timeout: float = 45.0) -> tuple[bytes | None, str, str]:
    """(data, name, why_not): the saved copy's bytes, or (None, "", the app's reason such as
    `not_on_disk` or `too_large`). Raises BridgeError only when the app can't be asked."""
    if int(rowid) <= 0 or not _ATTACHMENT_ID.match(str(attachment_id or "")):
        return None, "", "not_found"
    reply = await _ask("attachment", timeout, rowid=int(rowid), attachment_id=str(attachment_id))
    if reply.get("ok") is not True:
        return None, "", str(reply.get("error") or "attachment_failed")[:40]
    try:
        data = base64.b64decode(str(reply.get("data_b64") or ""), validate=True)
    except ValueError:
        return None, "", "bad_base64"
    return data, str(reply.get("name") or "")[:255], ""


# --- text recognition (MailMemoryReader kind `recognize`) ---------------------------

RECOGNIZE_MAX_BYTES = 4 * 1024 * 1024
RECOGNIZE_MAX_CHARS = 200_000
_RECOGNIZE_ERRORS = {"bad_image", "too_large", "failed"}


async def recognize_text(jpeg: bytes, timeout: float = 20.0) -> tuple[str, float]:
    """Text the app reads in one JPEG with Apple Vision: (text, confidence 0..1).

    `("", 0.0)` when it found no text. Raises BridgeError for everything else, including a
    non-JPEG or over-size input, which is refused here without asking the app. Only JPEGs
    Wisp's own child re-encoded from decoded pixels belong here."""
    if not isinstance(jpeg, (bytes, bytearray)) or bytes(jpeg[:3]) != b"\xff\xd8\xff":
        raise BridgeError("bad_image")
    if len(jpeg) > RECOGNIZE_MAX_BYTES:
        raise BridgeError("too_large")
    # `wait` tells the app how long we will wait, so it skips a request that sat in its queue
    # past that. An app from before this field drops a request carrying it (strict keys) and
    # the call then times out; that is acceptable because app and backend ship in one bundle.
    wait = max(1, min(60, math.ceil(timeout))) if math.isfinite(timeout) else 60
    reply = await _ask("recognize", timeout, image_b64=base64.b64encode(bytes(jpeg)).decode("ascii"), wait=wait)
    if reply.get("ok") is not True:
        error = str(reply.get("error") or "")
        if error == "no_text":
            return "", 0.0
        if error == "late":
            raise BridgeError("recognize_timeout")
        raise BridgeError(error if error in _RECOGNIZE_ERRORS else "recognize_failed")
    text = reply.get("text")
    confidence = reply.get("confidence")
    if not isinstance(text, str) or isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise BridgeError("recognize_failed")
    confidence = float(confidence)
    if confidence != confidence or confidence in (float("inf"), float("-inf")):
        raise BridgeError("recognize_failed")
    return text[:RECOGNIZE_MAX_CHARS], min(1.0, max(0.0, confidence))


# --- files texted in Messages (MailMemoryReader kinds `message_attachments`, `message_attachment`) ---

_MESSAGE_ATTACHMENT_ID = re.compile(r"[0-9]{1,18}")
_MESSAGE_ATTACHMENT_WHY = {"no_messages_db", "not_found", "not_on_disk", "too_large", "unreadable"}
MESSAGE_ATTACHMENT_LIMIT_MAX = 400
MESSAGE_HANDLES_MAX = 10
_MESSAGE_HANDLE_CHARS = 100
_MESSAGE_TERM_CHARS = 80      # the app accepts 1...100; a term is cut shorter here
_MESSAGE_LIMIT_DEFAULT = 200


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 for c in text)


def _clean_seconds(value) -> float:
    """A unix-seconds bound for the app: a finite number above zero, else 0.0 (no bound)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        number = float(value)
    except OverflowError:
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _clean_limit(value) -> int:
    """A row limit the app accepts (1...MESSAGE_ATTACHMENT_LIMIT_MAX); anything not int-like is the default."""
    if isinstance(value, bool):
        number = _MESSAGE_LIMIT_DEFAULT
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        number = int(value)
    else:
        number = _MESSAGE_LIMIT_DEFAULT
    return max(1, min(number, MESSAGE_ATTACHMENT_LIMIT_MAX))


def _strings(value) -> list[str]:
    return [x for x in value if isinstance(x, str)] if isinstance(value, (list, tuple)) else []


def _text_field(value, limit: int) -> str | None:
    """`value` cut to `limit` characters when it is a string, else None."""
    return value[:limit] if isinstance(value, str) else None


def _message_attachment_rows(reply: dict) -> list[dict]:
    out = []
    for r in reply.get("rows") or []:
        if not isinstance(r, dict):
            continue
        attachment_id = r.get("id")
        if not isinstance(attachment_id, str) or not _MESSAGE_ATTACHMENT_ID.fullmatch(attachment_id):
            continue
        ts = r.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts < 0:
            continue
        chat, sender = _text_field(r.get("chat"), 120), _text_field(r.get("sender"), 200)
        name, mime = _text_field(r.get("name"), 255), _text_field(r.get("mime"), 100)
        if chat is None or sender is None or mime is None or not name:
            continue
        from_me, on_disk, size = r.get("from_me"), r.get("on_disk"), r.get("size")
        if type(from_me) is not bool or type(on_disk) is not bool or type(size) is not int or size < 0:
            continue
        out.append({"id": attachment_id, "ts": float(ts), "chat": chat, "sender": sender, "from_me": from_me,
                    "name": name, "mime": mime, "size": size, "on_disk": on_disk})
    return out


async def read_message_attachment_index(terms: list[str], handles: list[str], since: float, until: float,
                                        limit: int = 200, timeout: float = 10.0) -> tuple[list[dict], bool]:
    """Files texted in Messages, one row per file, newest first, and whether the app capped the
    list. Every term must match the chat's name, the sender's handle or the file name; when
    `handles` is not empty the sender must be one of them. Raises BridgeError."""
    # The app refuses a request with a control character or an over-long entry, so clean here:
    # a control character in a term becomes a space; a handle with one is dropped.
    flat = ["".join(" " if _has_control(c) else c for c in t).strip()[:_MESSAGE_TERM_CHARS].strip() for t in _strings(terms)]
    cleaned = [t for t in flat if t][:ATTACHMENT_TERMS_MAX]
    wanted = list(dict.fromkeys(
        h for h in (x.strip()[:_MESSAGE_HANDLE_CHARS] for x in _strings(handles)) if h and not _has_control(h)
    ))[:MESSAGE_HANDLES_MAX]
    reply = await _ask("message_attachments", timeout, terms=cleaned, handles=wanted,
                       since=_clean_seconds(since), until=_clean_seconds(until), limit=_clean_limit(limit))
    if reply.get("ok") is not True:
        raise BridgeError(str(reply.get("error") or "message_attachments_failed")[:40])
    return _message_attachment_rows(reply), reply.get("capped") is True


async def read_message_attachment(attachment_id: str, timeout: float = 45.0) -> tuple[bytes | None, str, str]:
    """(data, name, why_not): one texted file's bytes, or (None, "", the app's reason: `not_found`,
    `not_on_disk`, `too_large`, `unreadable` or `no_messages_db`; any other reply, and bytes that are
    not valid base64, are `unreadable`). The request carries only the id, never a path. Raises BridgeError only when the app can't be asked."""
    if not isinstance(attachment_id, str) or not _MESSAGE_ATTACHMENT_ID.fullmatch(attachment_id):
        return None, "", "not_found"
    reply = await _ask("message_attachment", timeout, attachment_id=attachment_id)
    if reply.get("ok") is not True:
        error = str(reply.get("error") or "")
        return None, "", error if error in _MESSAGE_ATTACHMENT_WHY else "unreadable"
    try:
        data = base64.b64decode(str(reply.get("data_b64") or ""), validate=True)
    except ValueError:
        return None, "", "unreadable"
    return data, str(reply.get("name") or "")[:255], ""
