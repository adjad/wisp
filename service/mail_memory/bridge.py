"""Ask the app for Mail data, one request at a time (MailMemoryReader.swift answers).

The backend can't read Mail's files: Full Disk Access belongs to the app. So a
request is a non-durable `mail_memory_read` hub event carrying a random,
single-use `request_id`, and the app answers by POST to
/assistant/sync/mail_memory. A reply is accepted only for a request still
waiting here, of the kind that was asked, so nothing can push mail in unasked.
"""
from __future__ import annotations

import asyncio
import base64
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
