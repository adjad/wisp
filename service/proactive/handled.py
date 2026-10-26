"""Things the user handled themselves, outside Wisp.

A Before you ask card, a to-do or a Needs attention row about a conversation
leaves once the user answered that conversation on their own: a text sent in
Messages after its last incoming text, or mail sent (Gmail, Mail or a phone;
gmail_source._sync_sent) in reply to it. A reminder or calendar card leaves
once the same thing is on the user's own Reminders or Calendar.

Code only, no model: the cycle's next run would see the same thing, but it can
be an hour away. The sweep runs from the scheduler at most every SWEEP_EVERY_S.
A retired card is dismissed (its fingerprints suppressed, so it comes back only
when a new message changes the conversation); a reply to-do is marked done, so
the Brief shows it checked.
"""
from __future__ import annotations

import re
import time

from service.proactive import sources

SWEEP_EVERY_S = 60.0

# Cards whose work is the reply itself: answering the thread finishes them.
_REPLY_METHODS = {"email_reply", "text_reply", "read"}
_REPLY_TITLE = re.compile(r"^\s*(?:reply|respond|answer|get back|text|email|message|write back)\b", re.I)
# A calendar or reminder offer is finished by the user's own entry.
_SCHEDULE_METHODS = {"reminder", "calendar_event"}


def seen_entry(item: dict) -> dict | None:
    """What the sweep needs later about one mail or text item."""
    received = item.get("received_ts")
    if type(received) not in (int, float):
        return None
    if item.get("source") == "messages":
        conversation = (item.get("ref") or {}).get("conversation")
        if not conversation or item.get("group"):
            return None
        return {"source": "messages", "conversation": conversation, "received_ts": float(received)}
    if item.get("source") == "mail" and not item.get("critical"):
        ref = item.get("ref") or {}
        return {"source": "mail", "account": str(item.get("raw_account") or ref.get("account") or ""),
                "subject": str(ref.get("subject") or item.get("title") or ""),
                "message_id": str(ref.get("message_id") or ""), "received_ts": float(received)}
    return None


def latest_sent_texts(records) -> dict[str, float]:
    """The newest text the user sent, by conversation."""
    latest: dict[str, float] = {}
    for record in records:
        ts, context, text = record[:3]
        if str(text).partition(":")[0].strip() == "Me" and ts > latest.get(context, float("-inf")):
            latest[context] = ts
    return latest


def answered_refs(seen: dict[str, dict], *, texts: dict[str, float], sent: list[dict]) -> set[str]:
    """Item ids (card source refs) the user answered after their last incoming message."""
    out = set()
    for ref, entry in seen.items():
        if entry.get("source") == "messages":
            if texts.get(entry.get("conversation"), float("-inf")) > entry["received_ts"]:
                out.add(ref)
        elif entry.get("source") == "mail" and sent:
            if sources.answered_by_sent(sent, account=entry.get("account", ""), subject=entry.get("subject", ""),
                                        message_id=entry.get("message_id", ""), after=entry["received_ts"]):
                out.add(ref)
    return out


def _refs(record: dict) -> set[str]:
    refs = {str(s.get("ref")) for s in record.get("sources") or [] if isinstance(s, dict) and s.get("ref")}
    for fingerprint in record.get("fingerprints") or []:
        refs.add(str(fingerprint).rpartition(":")[0])
    return {r for r in refs if r}


def _reply_work(card: dict, todo: dict | None) -> bool:
    method = (card.get("action") or {}).get("method")
    return (method in _REPLY_METHODS or (todo or {}).get("kind") == "reply"
            or bool(_REPLY_TITLE.match(str(card.get("title") or ""))))


def _on_own_schedule(card: dict, sched: dict) -> bool:
    if (card.get("action") or {}).get("method") not in _SCHEDULE_METHODS:
        return False
    from service.proactive import judge
    quote = " ".join(str(s.get("quote") or "") for s in card.get("sources") or [] if isinstance(s, dict))
    return judge._schedule_duplicate(f"{card.get('title') or ''} {quote}", sched)


def retire(store, *, answered: set[str], sched: dict | None = None, now: float | None = None) -> dict:
    """Dismiss cards and finish to-dos the user handled; mark their threads
    answered. Returns what changed, for the log and tests."""
    now = time.time() if now is None else now
    state = store.state()
    todos = {t.get("id"): t for t in state.get("todos") or []}
    done_ids = set(store.done_todos(now)) if hasattr(store, "done_todos") else set()
    retired, finished = [], []
    for card in list(state.get("cards") or []):
        method = (card.get("action") or {}).get("method")
        if method in ("undo_add", "block_time"):
            continue                      # Wisp's own "ADDED" notice, and held work time
        todo = todos.get(card.get("todo_id"))
        by_reply = bool(_refs(card) & answered) and _reply_work(card, todo)
        if by_reply or (sched and _on_own_schedule(card, sched)):
            store.dismiss_card(card, now)
            retired.append(card["id"])
            if todo and todo.get("id") not in done_ids:
                store.set_todo_done(todo, True, now)
                done_ids.add(todo["id"])
                finished.append(todo["id"])
    for todo in todos.values():
        if todo.get("id") in done_ids or todo.get("source") not in ("mail", "messages"):
            continue
        if todo.get("kind") == "reply" and _refs(todo) & answered:
            store.set_todo_done(todo, True, now)
            done_ids.add(todo["id"])
            finished.append(todo["id"])
    threads = state.get("threads") or []
    marked = [t["id"] for t in threads if t.get("id", "").removeprefix("thread:") in answered and not t.get("replied")]
    if marked:
        with store._lock:
            fresh = store.state()
            fresh["threads"] = [{**t, "replied": True, "needs_reply": False, "handled": True}
                                if t.get("id") in marked else t for t in fresh.get("threads") or []]
            store._write("state.json")
    return {"cards": retired, "todos": finished, "threads": marked}


_last_sweep = 0.0


def sweep(store=None, *, now: float | None = None, texts=None, sent=None, sched=None) -> dict:
    """Read what the user sent and retire what it handled. Never fatal."""
    from service.proactive.store import store as default_store
    store = store or default_store()
    now = time.time() if now is None else now
    seen = store.state().get("seen") or {}
    if texts is None:
        texts = latest_sent_texts(sources.text_records())
    if sent is None:
        sent = sources.sent_rows()
    answered = answered_refs(seen, texts=texts, sent=sent)
    return retire(store, answered=answered, sched=sched, now=now)


def due(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - _last_sweep >= SWEEP_EVERY_S


def mark_swept(now: float | None = None) -> None:
    global _last_sweep
    _last_sweep = time.time() if now is None else now
