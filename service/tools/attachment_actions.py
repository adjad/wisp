"""save_email_attachment: put ONE file from an email or from Messages into a folder.

The finding, fetching, risk check and writing live in service/attachment_save.py.
This module is the tool the model sees and the two steps around the card:

1. BEFORE the card, the loop calls `prepare_save_args`. It resolves the dates the
   way the read tools do, asks `attachment_save.prepare` to settle on exactly one
   file, and keeps what comes back (`frozen`, the engine's description of that one
   file, and `card`, the line the person approves) on the server, under a random
   one-use token. Only the token goes into the action's arguments. The model
   never writes `frozen`, `card` or a token: `prepare_save_args` rebuilds the
   arguments from the plain fields only, the schema has no `frozen` or `card` at
   all, and the tool refuses a token it did not mint for these same arguments.
2. On yes, the tool takes the token (once) and calls `attachment_save.save` with
   exactly the frozen description.

A step that stops before anything is written is a typed not-done result
(registry.not_done), so a refusal never locks the chat as "unverified".
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from collections import OrderedDict

from service.tools.email_attachments import _span
from service.tools.email_tools import _CORPUS_MAIL_IDENTIFIER
from service.tools import untrusted
from service.tools.registry import not_done, register, saved

SAVE_TOOL = "save_email_attachment"

# The arguments the model may give. Anything else is dropped before the card.
_PLAIN_ARGS = ("query", "name", "period", "day", "account", "source", "folder")

# Prepared files by token: (made at, the arguments they were made for, frozen, card).
# Server-side only; bounded; each token works once and for a few minutes.
_PREPARED: "OrderedDict[str, tuple[float, str, dict, str]]" = OrderedDict()
_PREPARED_LOCK = threading.Lock()
_PREPARED_MAX = 16
_PREPARED_TTL = 900.0


def _clean(args: dict) -> dict:
    """The model's own fields, as short plain strings; nothing else."""
    out: dict = {}
    for key in _PLAIN_ARGS:
        value = (args or {}).get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:300]
    out["source"] = "messages" if out.get("source", "").lower() == "messages" else "mail"
    return out


def _key(clean: dict) -> str:
    return json.dumps({k: clean.get(k, "") for k in _PLAIN_ARGS}, sort_keys=True)


def _remember(clean: dict, frozen: dict, card: str) -> str:
    token = "att_" + secrets.token_hex(16)
    now = time.monotonic()
    with _PREPARED_LOCK:
        for old in [t for t, v in _PREPARED.items() if now - v[0] > _PREPARED_TTL]:
            del _PREPARED[old]
        while len(_PREPARED) >= _PREPARED_MAX:
            _PREPARED.popitem(last=False)
        _PREPARED[token] = (now, _key(clean), frozen, card)
    return token


def card_for(args: dict) -> str | None:
    """The card line made for this action's token, or None. Does not use the token up."""
    token = (args or {}).get("prepared")
    if not isinstance(token, str):
        return None
    with _PREPARED_LOCK:
        entry = _PREPARED.get(token)
    return entry[3] if entry else None


def _take(token, clean: dict) -> dict | None:
    """The frozen file for this token, once, only for the arguments it was made for."""
    if not isinstance(token, str):
        return None
    with _PREPARED_LOCK:
        entry = _PREPARED.pop(token, None)
    if entry is None or time.monotonic() - entry[0] > _PREPARED_TTL or entry[1] != _key(clean):
        return None
    return entry[2]


_CORPUS_REFUSAL = "Corpus mail scope rejected: saving attachments is outside the corpus scope."


def _stopped(reason: str, question: str = "") -> str:
    reason = untrusted.neutralize(" ".join(str(reason or "").split())[:400]) or "that file could not be settled on"
    return not_done(f"(NOT saved — {reason} Nothing was saved.)",
                    question=untrusted.neutralize(" ".join(str(question or "").split())[:600]) or reason)


async def prepare_save_args(args: dict) -> tuple[dict, str | None]:
    """Settle ONE file before the card. (arguments to run, None) or (args, a not-done result)."""
    if _CORPUS_MAIL_IDENTIFIER.get() is not None:
        return args, _stopped(_CORPUS_REFUSAL)
    clean = _clean(args)
    span = _span(clean.get("period"), clean.get("day"))
    if isinstance(span, str):
        return args, _stopped(span)
    start, end, _label = span
    try:
        from service import attachment_save
    except ImportError:
        return args, _stopped("saving attachments is not available in this build.")
    try:
        found = await attachment_save.prepare(
            query=clean.get("query"), name=clean.get("name"), start=start, end=end,
            account=clean.get("account"), source=clean["source"], folder=clean.get("folder"))
    except Exception:  # noqa: BLE001 — a failed look-up is a refusal, never a crash
        return args, _stopped("I couldn't look that file up just now.")
    if (not isinstance(found, dict) or found.get("ok") is not True
            or not isinstance(found.get("frozen"), dict) or not isinstance(found.get("card"), str)
            or not found["card"].strip()):
        found = found if isinstance(found, dict) else {}
        return args, _stopped(found.get("reason") or "", found.get("question") or "")
    try:
        frozen = json.loads(json.dumps(found["frozen"], allow_nan=False))
    except (TypeError, ValueError):
        return args, _stopped("the file could not be described safely.")
    return {**clean, "prepared": _remember(clean, frozen, found["card"].strip())}, None


@register(
    SAVE_TOOL,
    "Save ONE file attached to an email, or sent in Messages, into a folder (Downloads "
    "by default) and show it in Finder. It is saved only after the user confirms on a "
    "card that names the file and the folder; a risky file is refused; Wisp never opens "
    "or runs it. Pass `query` "
    "(who, or the subject) and `name` (part of the file's name) to pick it; if several "
    "match it asks which. To send an email on with its files, use forward_email.",
    {"type": "object",
     "properties": {
         "query": {"type": "string",
                   "description": "who sent it, or what the email or chat is about"},
         "name": {"type": "string",
                  "description": "part of the file's name, e.g. 'invoice' or '.pdf'"},
         "period": {"type": "string",
                    "description": "a date range for when it was sent, e.g. 'last week'"},
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD'"},
         "account": {"type": "string",
                     "description": "only this linked mail account"},
         "source": {"type": "string", "enum": ["mail", "messages"],
                    "description": "'mail' (default) or 'messages' for a file sent in Messages"},
         "folder": {"type": "string",
                    "description": "where to save it: a folder in the user's home, e.g. "
                                   "'~/Documents'; omit for Downloads"},
         "prepared": {"type": "string",
                      "description": "set by Wisp after it picks the file; never invent this"},
     }},
    category="fs_write",
    aliases=["save the attached PDF to my downloads", "download the attachment the landlord sent",
             "keep a copy of the attachment in my documents folder"],
)
async def save_email_attachment(query: str | None = None, name: str | None = None,
                                period: str | None = None, day: str | None = None,
                                account: str | None = None, source: str = "mail",
                                folder: str | None = None, prepared: str | None = None) -> str:
    if _CORPUS_MAIL_IDENTIFIER.get() is not None:
        return _stopped(_CORPUS_REFUSAL)
    clean = _clean({"query": query, "name": name, "period": period, "day": day,
                    "account": account, "source": source, "folder": folder})
    frozen = _take(prepared, clean)
    if frozen is None:
        return _stopped("this save was not set up and approved first.")
    try:
        from service import attachment_save
    except ImportError:
        return _stopped("saving attachments is not available in this build.")
    result = await attachment_save.save(frozen)
    if isinstance(result, dict) and result.get("ok") is True and isinstance(result.get("text"), str):
        return saved(untrusted.neutralize(result["text"]))
    reason = untrusted.neutralize(str(result.get("reason") or "")) if isinstance(result, dict) else ""
    if isinstance(result, dict) and result.get("wrote") is False:
        # The engine says it stopped before leaving anything behind.
        return _stopped(reason or "the file could not be saved.")
    return f"(error: the save did not finish cleanly: {reason[:300] or 'unknown'}. Check the folder before trying again.)"
