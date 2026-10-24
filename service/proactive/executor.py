"""Executor: fires a card only on the user's tap, through verified paths only.

- ``email_reply``: W3's ``send_reviewed_reply(envelope, body)`` in
  service/assistant/email_reply_api.py (lazy import; a clear
  ``send_unavailable`` result when it isn't there).
- ``text_reply``: the same core as ``POST /assistant/send_message_draft``
  (service/main.py): a literal handle, ``human_reviewed_content``,
  ``send_message``, and an audit line.
- ``reminder``: the verified native reminder write the attention capture's
  live mode uses (outbox ``create_reminder`` with a before-dispatch policy
  check).
- ``calendar_event``: attention_calendar.create_calendar, approved for exactly
  the event the card showed.
- ``add_to_day``: assistant_store.today_save_task.
- ``read``: nothing to do; the card is marked handled.

IMPORTANT: the recipient is the card's own, taken from the source when the
card was prepared. A request may repeat it, never change it. Only the draft's
words come from the request (the user edited them). A card is removed only
after the effect is verified; every fire is audit-logged.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime
from zoneinfo import ZoneInfo

_in_flight: set[str] = set()


def _audit(event: str, **fields) -> None:
    try:
        from service.safety.audit import audit
        audit(event, **fields)
    except Exception:  # noqa: BLE001 — auditing never blocks a result
        pass


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


# --- each method -------------------------------------------------------------------


# Sealed reply envelopes, by card id: in this process's memory only, never on
# disk. A restart forgets them, and the fire tap freezes a fresh one.
_ENVELOPES: dict[str, tuple[str, float]] = {}
_FRESH_MARGIN_S = 60


def remember(card_id: str, envelope, expires_at) -> None:
    if isinstance(envelope, str) and envelope and type(expires_at) in (int, float):
        _ENVELOPES[card_id] = (envelope, float(expires_at))


def _shown(action: dict) -> tuple:
    norm = lambda values: sorted(str(v).strip().casefold() for v in values or [])  # noqa: E731
    return norm(action.get("to")), norm(action.get("cc")), str(action.get("subject") or "").strip()


def _show(card: dict, view: dict, store) -> dict:
    """Keep the card, now showing the recipients and subject Mail reported."""
    action = card["action"]
    action["to"], action["cc"] = list(view.get("to") or []), list(view.get("cc") or [])
    action["subject"] = str(view.get("subject") or action.get("subject") or "")
    action["recipient"] = ", ".join(action["to"] + action["cc"])
    remember(card["id"], view.get("envelope"), view.get("expires_at"))
    if store is not None:
        store.update_card(card)
    return {"recipient": action["recipient"], "to": action["to"], "cc": action["cc"], "subject": action["subject"]}


async def _email_reply(card: dict, draft: str, store=None) -> dict:
    """W3's send core, called only from the fire tap with the card's exact
    draft. Never retried. "confirm" (Mail's real recipients differ) sends
    nothing: the card stays, now showing those recipients, for a fresh tap.
    An envelope from another process or near its expiry is frozen again; if
    the fresh one shows other recipients or another subject, that is a
    confirm too, never a silent send."""
    try:
        from service.assistant.email_reply_api import freeze_reply, send_reviewed_reply
    except ImportError:
        return {"ok": False, "status": "send_unavailable",
                "error": "Email replies from Wisp aren't available in this build. Open the email in Mail to reply."}
    action = card["action"]
    held = _ENVELOPES.get(card["id"])
    if held is None or held[1] - time.time() < _FRESH_MARGIN_S:
        view = freeze_reply(action.get("reply_ref"))
        if not (isinstance(view, dict) and view.get("ok") and view.get("can_send") and view.get("envelope")):
            return {"ok": False, "status": "send_unavailable",
                    "error": (view or {}).get("reason") or "This email can't be replied to from Wisp.",
                    "open_url": (view or {}).get("open_url", "")}
        before = _shown(action)
        shown = _show(card, view, store)
        if _shown(action) != before:
            return {"ok": False, "status": "confirm", "error": "recipients_changed",
                    "result": f"This reply would go to {shown['recipient']}. Tap again to confirm.", **shown}
        held = _ENVELOPES[card["id"]]
    _ENVELOPES.pop(card["id"], None)     # single use, whatever happens next
    result = await send_reviewed_reply(held[0], draft)
    if not isinstance(result, dict):
        return {"ok": False, "status": "unknown", "error": "The send result was not understood."}
    if result.get("status") == "confirm" and result.get("envelope"):
        result = {**{k: v for k, v in result.items() if k != "envelope"}, **_show(card, result, store)}
    return {**result, "ok": result.get("status") == "sent"}


def _text_policy_problem(to: str, draft: str) -> str | None:
    """The safety policy floor for a text. DENY (view-only mode, a grant that
    forbids it) refuses; CONFIRM is satisfied by the user's tap on this exact
    card, which showed the recipient and the words."""
    try:
        from service.safety import Tier, decide
        decision = decide("messages_send", {"to": to, "text": draft}, tool="send_message")
    except Exception as exc:  # noqa: BLE001 — a policy that can't be read refuses
        return f"safety policy unavailable ({type(exc).__name__})"
    if decision.tier is Tier.DENY:
        return decision.reason or "The safety policy doesn't allow sending this text."
    return None


async def _text_reply(card: dict, draft: str) -> dict:
    """The send_message_draft core: a literal handle, the policy floor, and a
    human-reviewed body. An unconfirmed send is "unknown", never "failed"."""
    from service.tools.action_tools import human_reviewed_content, send_message
    from service.tools.registry import _literal_message_recipient, _OutboundUnknownResult
    to = _literal_message_recipient(card["action"].get("recipient"))
    if to is None:
        return {"ok": False, "status": "blocked", "error": "This card has no literal phone number or address to text."}
    if problem := _text_policy_problem(to, draft):
        _audit("deny", tool="send_message", source="proactive_card", reason=problem)
        return {"ok": False, "status": "blocked", "error": problem}
    with human_reviewed_content():
        result = await send_message(to=to, text=draft)
    ok = isinstance(result, str) and result.startswith("Message sent to ")
    unknown = isinstance(result, _OutboundUnknownResult)
    _audit("draft_card_send", tool="send_message", source="proactive_card",
           args={"to": to, "text_sha256": _sha(draft)}, result=str(result)[:200], ok=ok)
    return {"ok": ok, "status": "succeeded" if ok else "unknown" if unknown else "failed", "result": str(result)}


def _reminder_problem() -> str | None:
    from service.safety import read_only
    if read_only():
        return "View-only mode: reminders can't be created."
    from service.assistant.attention_runner import _policy_block
    return _policy_block(kind="reminder")


async def _reminder(card: dict) -> dict:
    from service.assistant.outbox import request
    envelope = card["action"]["envelope"]
    title, due = envelope.get("title"), envelope.get("due_ts")
    if not isinstance(title, str) or not title.strip() or type(due) not in (int, float):
        return {"ok": False, "status": "blocked", "error": "This reminder has no title or time."}
    if problem := _reminder_problem():
        return {"ok": False, "status": "blocked", "error": problem}
    result = await request("create_reminder", {"title": title.strip()[:200], "due_ts": float(due),
                                               "commitment_kind": "reminder"},
                           before_dispatch=_reminder_problem)
    _audit("allow", tool="add_reminder", source="proactive_card",
           status="succeeded" if result.get("ok") is True else result.get("status", "unknown"),
           args={"title_sha256": _sha(title), "due_ts": due})
    return {**result, "ok": result.get("ok") is True}


async def _calendar_event(card: dict, still_current) -> dict:
    from service.assistant.attention_calendar import approve_capture, create_calendar
    event = card["action"].get("event") or {}
    plan = {"capture_key": "capture:proactive:" + card["id"], "source_id": "proactive:" + card["id"],
            "title": event.get("title"), "day": event.get("day"), "timezone": event.get("timezone"),
            "start_ts": event.get("start_ts"), "duration_min": event.get("duration_min"),
            "all_day": bool(event.get("all_day")), "automatic": False}
    if plan["all_day"]:
        plan["start_ts"] = plan["duration_min"] = None
    # The user's tap on this exact card is the approval for exactly this event.
    result = await create_calendar(plan, approval=approve_capture(plan), source_current=still_current)
    return {**result, "ok": result.get("ok") is True}


def _local_day(now: float) -> tuple[str, str]:
    try:
        from service.attention.detectors import local_timezone
        zone = local_timezone() or "UTC"
    except Exception:  # noqa: BLE001
        zone = "UTC"
    return datetime.fromtimestamp(now, ZoneInfo(zone)).date().isoformat(), zone


def add_to_day(title: str, due_ts: float | None, *, now: float | None = None, day: str | None = None,
               timezone: str | None = None, store=None) -> dict:
    now = time.time() if now is None else now
    if store is None:
        from service.assistant.store import assistant_store as store
    if day is None or timezone is None:
        day, timezone = _local_day(now)
    values = {"title": str(title or "").strip()[:500], "day": day, "timezone": timezone, "kind": "task",
              "duration_minutes": 30, "priority": 2}
    if type(due_ts) in (int, float):
        values["due_ts"] = float(due_ts)
    try:
        task = store.today_save_task(values)
    except (ValueError, KeyError) as exc:
        return {"ok": False, "status": "blocked", "error": str(exc)}
    return {"ok": True, "status": "succeeded", "task_id": task.get("id")}


# --- fire -----------------------------------------------------------------------------


async def fire(card_id: str, draft: str | None = None, recipient: str | None = None, *,
               store=None, methods: dict | None = None, now: float | None = None) -> dict:
    """Run one card's action. The card is removed only when the result is a
    verified success; anything else leaves it for another try.

    `methods` lets tests replace the native effects; production uses the
    verified paths above."""
    from service.proactive.store import store as default_store
    store = store or default_store()
    now = time.time() if now is None else now
    card = store.card(card_id)
    if card is None:
        return {"ok": False, "status": "not_found", "error": "That card is gone. Refresh the Brief."}
    action = card.get("action") or {}
    method = action.get("method")
    if recipient is not None and str(recipient).strip().casefold() != str(action.get("recipient") or "").strip().casefold():
        _audit("deny", tool="proactive_fire", card=card_id, method=method, reason="recipient_changed")
        return {"ok": False, "status": "blocked",
                "error": "The recipient comes from the original message and can't be changed here."}
    words = action.get("draft", "") if draft is None else draft
    if method in ("email_reply", "text_reply"):
        if type(words) is not str or not words.strip():
            return {"ok": False, "status": "blocked", "error": "The message is empty."}
        if len(words) > 5000:
            return {"ok": False, "status": "blocked", "error": "The message is too long."}
    if action.get("locked") == "unknown" or store.locked(card, now):
        where = "Mail" if method == "email_reply" else "Messages"
        return {"ok": False, "status": "locked",
                "error": f"Check {where} before sending again: the last send wasn't confirmed. "
                         "Dismiss this card or mark it handled."}
    if card_id in _in_flight:
        return {"ok": False, "status": "in_progress", "error": "This card is already being sent."}
    _in_flight.add(card_id)
    try:
        result = await _run(card, method, words, store, methods or {}, now)
    except Exception as exc:  # noqa: BLE001 — an unexpected failure is never a success
        import traceback
        traceback.print_exc()
        result = {"ok": False, "status": "unknown" if method in ("email_reply", "text_reply") else "failed",
                  "error": type(exc).__name__}
    finally:
        _in_flight.discard(card_id)
    ok = result.get("ok") is True
    if not ok and result.get("status") == "unknown" and method in ("email_reply", "text_reply"):
        # It may have gone out: never offer the same send again.
        store.mark_unknown(card, now)
        current = store.card(card_id)
        if current is not None:
            current["action"]["locked"] = "unknown"
            store.update_card(current)
    _audit("proactive_fire", tool=method, card=card_id, account=card.get("account", ""), ok=ok,
           status=result.get("status", "succeeded" if ok else "failed"),
           args={"recipient": action.get("recipient", ""), "draft_sha256": _sha(words or ""),
                 "edited": words != action.get("draft", "")})
    if ok:
        store.record_fire(card, result, now)
    return {**result, "ok": ok, "card_id": card_id, "removed": ok}


async def _run(card: dict, method: str, words: str, store, methods: dict, now: float) -> dict:
    still_current = lambda: store.card(card["id"]) is not None  # noqa: E731
    if method == "email_reply":
        result = await (methods["email_reply"](card, words) if "email_reply" in methods
                        else _email_reply(card, words, store))
    elif method == "text_reply":
        result = await (methods["text_reply"](card, words) if "text_reply" in methods else _text_reply(card, words))
    elif method == "reminder":
        call = methods.get("reminder") or _reminder
        result = await call(card)
        if result.get("ok") is not True and result.get("status") == "not_attempted":
            result = await call(card)          # one retry, only when nothing was dispatched
    elif method == "calendar_event":
        call = methods.get("calendar_event") or (lambda c: _calendar_event(c, still_current))
        result = await call(card)
    elif method == "add_to_day":
        envelope = card["action"].get("envelope") or {}
        call = methods.get("add_to_day")
        result = call(card) if call else add_to_day(envelope.get("title") or card.get("title"), envelope.get("due_ts"),
                                                     now=now)
        if result.get("ok") is not True and result.get("status") != "blocked":
            result = call(card) if call else add_to_day(envelope.get("title") or card.get("title"),
                                                         envelope.get("due_ts"), now=now)
    elif method == "read":
        result = {"ok": True, "status": "succeeded"}
    else:
        return {"ok": False, "status": "blocked", "error": f"Unknown action {method!r}."}
    # A reply that also proposed a time adds the hold after the send succeeds;
    # the send is what the card promised, so a failed hold is reported, not undone.
    if result.get("ok") is True and method in ("email_reply", "text_reply") and card["action"].get("event"):
        try:
            call = methods.get("calendar_event") or (lambda c: _calendar_event(c, still_current))
            hold = await call(card)
            result = {**result, "event": {"ok": hold.get("ok") is True, "status": hold.get("status")}}
        except Exception as exc:  # noqa: BLE001
            result = {**result, "event": {"ok": False, "status": type(exc).__name__}}
    return result
