"""Loader for the Aria demo's seed data (pure stdlib, read-only files).

Sources:

* ``service/demo/founder-demo.json`` — notes n01-n12, reminders r01-r22,
  contacts c01-c14 (unchanged founder fixtures; still the single source for
  those three types);
* this package's JSON files (schemas in README.md, checked by validate.py):
  ``contacts_extra.json`` (c15-c19), ``mail.json`` (threads + messages with
  absolute ``-07:00`` dates), ``messages.json`` (chats + messages),
  ``reply_templates.json`` (deterministic scripted replies).

Mail and message dates are anchored to ``as_of`` (2026-10-11 17:30 PT). The
world shifts them by whole weeks at seed time (``week_shift``) so the demo
stays fresh on later days and keeps its weekday rhythm. ``validate()``
enforces the fictional-data rules again at load: reserved email domains
(example.com/.org/.net, *.example) and 555-01xx phone numbers only.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
FOUNDER = HERE.parent / "founder-demo.json"
_EMAIL_OK = re.compile(r"^[^@\s]+@(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net)|[a-z0-9-]+\.example)$", re.I)
_PHONE_OK = re.compile(r"55501\d\d$")

def email_ok(value: str) -> bool:
    return bool(_EMAIL_OK.match((value or "").strip()))


def phone_ok(value: str) -> bool:
    return bool(_PHONE_OK.search(re.sub(r"\D", "", value or "")))


def _read(name: str):
    raw = (HERE / name).read_bytes()
    if len(raw) > 4_000_000:
        raise ValueError(f"{name} exceeds its size bound")
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("synthetic") is False:
        raise ValueError(f"{name} must be explicitly fictional")
    return data


def _ts(value: str) -> float:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"Seed date {value!r} needs an offset")
    return dt.timestamp()


def _who(row) -> dict:
    row = row or {}
    return {"name": row.get("name") or "", "email": row.get("email") or "",
            "contact_id": row.get("contact_id")}


def normalize_mail(raw: dict) -> list[dict]:
    """Threads in file order, each with its messages in date order."""
    by_thread: dict[str, list[dict]] = {}
    for m in raw.get("messages", []):
        attachments = [str(a) for a in m.get("attachments") or []]
        body = m["body"] + (("\n\nAttachments: " + ", ".join(attachments)) if attachments else "")
        by_thread.setdefault(m["thread_id"], []).append({
            "id": m["id"], "mailbox": m["mailbox"], "from": _who(m.get("from")),
            "to": [_who(t) for t in m.get("to") or []], "cc": [_who(t) for t in m.get("cc") or []],
            "subject": m.get("subject") or "", "body": body, "ts": _ts(m["date"]),
            "unread": bool(m.get("unread")), "flagged": bool(m.get("flagged"))})
    threads = []
    for t in raw.get("threads", []):
        msgs = sorted(by_thread.get(t["id"], []), key=lambda m: m["ts"])
        threads.append({"id": t["id"], "subject": t.get("subject") or "", "messages": msgs})
    return threads


def normalize_messages(raw: dict) -> list[dict]:
    """Chats in file order; each message names its sender ("me" or a contact id)."""
    by_chat: dict[str, list[dict]] = {}
    for m in raw.get("messages", []):
        by_chat.setdefault(m["chat_id"], []).append({
            "id": m["id"], "sender": m.get("sender") or ("me" if m.get("is_from_me") else ""),
            "direction": "out" if m.get("is_from_me") else "in", "text": m["text"],
            "ts": _ts(m["date"]), "unread": not m.get("read", True) and not m.get("is_from_me")})
    return [{"id": c["id"], "name": c["name"], "participants": list(c.get("participants") or []),
             "is_group": bool(c.get("is_group")),
             "messages": sorted(by_chat.get(c["id"], []), key=lambda m: m["ts"])}
            for c in raw.get("chats", [])]


@lru_cache(maxsize=1)
def _bundle_text() -> str:
    founder = json.loads(FOUNDER.read_bytes())
    if founder.get("synthetic") is not True:
        raise ValueError("Founder fixtures must be explicitly fictional")
    mail = _read("mail.json")
    messages = _read("messages.json")
    bundle = {
        "founder": {k: founder[k] for k in ("notes", "reminders", "contacts")},
        "as_of": _ts(mail.get("as_of") or messages.get("as_of")),
        "mail_threads": normalize_mail(mail),
        "chats": normalize_messages(messages),
        "contacts_extra": list(_read("contacts_extra.json").get("contacts", [])),
        "reply_templates": _read("reply_templates.json"),
    }
    problems = validate(bundle)
    if problems:
        raise ValueError("Aria demo data failed validation: " + "; ".join(problems[:5]))
    return json.dumps(bundle, ensure_ascii=False)


def load() -> dict:
    """A caller-owned copy of the validated seed bundle."""
    return json.loads(_bundle_text())


WEEK_S = 7 * 86400


def week_shift(as_of: float, now: float) -> float:
    """Seconds to add to every seeded mail/message date: whole weeks only, so
    weekday rhythms hold, and never past ``now``. DST changes between the
    anchor and today can move wall-clock times by an hour; accepted."""
    return math.floor((now - as_of) / WEEK_S) * WEEK_S


def validate(bundle: dict) -> list[str]:
    problems: list[str] = []
    contacts = bundle["founder"]["contacts"] + bundle["contacts_extra"]
    ids = [c.get("id") for c in contacts]
    if len(set(ids)) != len(ids) or not all(ids):
        problems.append("contact ids must be unique and nonempty")
    known = set(ids)
    for c in contacts:
        if c.get("email") and not email_ok(c["email"]):
            problems.append(f"contact {c.get('id')} email is not a reserved domain")
        if c.get("phone") and not phone_ok(c["phone"]):
            problems.append(f"contact {c.get('id')} phone is not 555-01xx")
    for t in bundle["mail_threads"]:
        if not t["messages"]:
            problems.append(f"mail thread {t['id']} is empty")
        for m in t["messages"]:
            for who in [m["from"], *m["to"], *m["cc"]]:
                if not email_ok(who["email"]):
                    problems.append(f"mail {m['id']} address {who['email']!r} is not a reserved domain")
                if who["contact_id"] and who["contact_id"] not in known:
                    problems.append(f"mail {m['id']} names unknown contact {who['contact_id']}")
            if m["mailbox"] not in {"inbox", "sent", "archive"}:
                problems.append(f"mail {m['id']} mailbox must be inbox, sent or archive")
            if m["ts"] > bundle["as_of"] + 86400:
                problems.append(f"mail {m['id']} is dated after as_of")
    for c in bundle["chats"]:
        for p in c["participants"]:
            if p != "me" and p not in known:
                problems.append(f"chat {c['id']} names unknown contact {p}")
        for m in c["messages"]:
            if m["sender"] != "me" and m["sender"] not in known:
                problems.append(f"message {m['id']} sender {m['sender']!r} is unknown")
            if m["ts"] > bundle["as_of"] + 86400:
                problems.append(f"message {m['id']} is dated after as_of")
    rt = bundle["reply_templates"]
    if not rt.get("generic"):
        problems.append("reply_templates.generic is required")
    for cid in rt.get("by_contact", {}):
        if cid not in known:
            problems.append(f"reply templates name unknown contact {cid}")
    return problems


def reply_template(channel: str, contact_id: str | None, sent_text: str) -> dict:
    """The scripted reply for an outgoing ``channel`` ("message" or "mail")
    text, chosen exactly as reply_templates.json's ``selection`` describes:
    filter by channel ("any" matches both); the first template (file order)
    whose when_contains keyword appears in the text wins; otherwise pick among
    keyword-less templates by sum(ord(c)) % n; a contact without templates
    uses ``generic`` by the same rules. No model is involved."""
    rt = load()["reply_templates"]
    text = (sent_text or "").casefold()

    def choose(pool: list[dict]) -> dict | None:
        pool = [t for t in pool if t.get("channel") in ("any", channel)]
        for t in pool:
            if any(k.casefold() in text for k in t.get("when_contains") or []):
                return t
        plain = [t for t in pool if not t.get("when_contains")]
        if not plain:
            return None
        return plain[sum(ord(c) for c in (sent_text or "")) % len(plain)]

    return (choose(rt.get("by_contact", {}).get(contact_id or "", [])) or choose(rt["generic"])
            or {"text": "Thanks, got it.", "delay_seconds": 30})


def reply_text(channel: str, contact: dict, sent_text: str) -> str:
    return reply_template("mail" if channel == "email" else channel, contact.get("id"), sent_text)["text"]
