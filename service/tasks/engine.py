"""Persistent coordinator for typed reminder tasks."""
from __future__ import annotations

from datetime import datetime, timedelta
import re
import time

from service.reminder_intent import is_unsupported_time_answer
from service.tasks.compiler import compile_task
from service.tasks.models import (
    CHANNEL_FOR_INTENT, OUTBOUND_INTENTS, SlotValue, TaskPlan, TaskTurn,
    TemporalValue,
)
from service.tasks.planner import InvalidTaskPlan, plan_task
from service.tasks.temporal import (
    apply_lead, parse_lead_seconds, resolve_event_reference, resolve_named_time,
)


_CANCEL = re.compile(
    r"^\s*(?:no|nope|never\s*mind|nevermind|cancel(?:\s+it)?|stop|forget\s+it)\s*[.!]?\s*$",
    re.I)
_CORRECTION_WITHOUT_TIME = re.compile(
    r"\b(?:mine|myself|for\s+me|i\s+need\s+it|reminders?(?:\.app)?|do\s+it\s+on)\b",
    re.I)
_RETRY = re.compile(
    r"^\s*(?:yes|ok(?:ay)?|sure|retry|try again|do it|go ahead)\s*[.!]?\s*$",
    re.I)
_UNRELATED_SUBJECT_REPLY = re.compile(
    r"^\s*(?:(?:can|could|would)\s+you\s+|please\s+)?"
    r"(?:what|when|where|who|why|how|show|check|search|find|open|play|set|create|add|send|email|text)\b|"
    r"\b(?:weather|inbox)\b",
    re.I)
_CONTEXT_DAY_CORRECTION = re.compile(
    r"^\s*(?:(?:i\s+mean|actually|make\s+(?:it|that)(?:\s+reminder)?)\s+)?"
    r"(?P<day>today|tomorrow)(?:\s+(?:please|sorry|instead))?\s*[.!]?\s*$", re.I)
_REMINDER_SOURCES = frozenset({"manual", "reminders"})
_LITERAL_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_LITERAL_PHONE = re.compile(r"^\+?\d[\d\s().\-]{6,}$")
_SELF = re.compile(r"^\s*(?:me|myself|my\s*self)\s*$", re.I)
_CHANNEL_WORD = re.compile(
    r"\b(?P<channel>imessages?|texts?|sms|e-?mails?)\b", re.I)


def _default_contacts_resolver(name: str) -> list[dict]:
    from service.tools.imessage_tools import find_contacts
    return find_contacts(name)


def _channel_from_word(word: str) -> str:
    return "email" if word.casefold().replace("-", "").startswith("email") \
        else "messages"


def _channel_correction(plan: TaskPlan, text: str) -> str:
    """The new channel a short reply asks for, or "" if it asks for none."""
    if plan.intent not in OUTBOUND_INTENTS:
        return ""
    reply = text.strip(" .")
    if len(reply.split()) > 8:
        return ""
    word = _CHANNEL_WORD.search(reply)
    if not word:
        return ""
    channel = _channel_from_word(word.group("channel"))
    return channel if channel != str(plan.channel.value or "") else ""


def _candidate_rows(plan: TaskPlan) -> list[dict]:
    slot = plan.parameters.get("recipient_candidates")
    value = slot.value if slot else None
    return [row for row in (value or []) if isinstance(row, dict)]


def _ask_for_recipient(plan: TaskPlan, message: str, event: str, *,
                       candidates: list[dict] | None = None) -> tuple[str, str]:
    """Ambiguity is a QUESTION, never a failed send.

    The legacy path resolved the destination inside the executor and reported
    ambiguity as finish("failed", "Nothing sent. …?"), which is why
    workflows/engine.py needed a branch to repair a failed plan without losing
    its sources.  Here the plan simply stays open on a typed missing slot.
    """
    plan.resolved_recipient = None
    plan.missing_slots = ["recipient.address"]
    plan.status = "waiting_for_input"
    if candidates is not None:
        plan.parameters["recipient_candidates"] = SlotValue(
            candidates, "resolved")
    return message, event


_FILLER = frozenset({
    "the", "one", "ones", "use", "using", "my", "please", "address", "account",
    "that", "it", "this", "send", "to", "at", "her", "him", "them", "instead",
    # Channel nouns are how people point at a candidate ("the work email"),
    # not part of the handle they are pointing at.
    "email", "e-mail", "mail", "number", "text", "message", "phone",
})


def _match_tier(query: str, name: str) -> str:
    """How closely a contact name matched what the user actually typed.

    `find_contacts` matches exact -> whole-word -> substring and returns the
    first tier that hits, so a single result is NOT evidence of a close match:
    "trish" returns "Trishy" as confidently as "Trishy" does.
    """
    wanted, found = _norm(query), _norm(name)
    if wanted == found:
        return "exact"
    if wanted and re.search(rf"\b{re.escape(wanted)}\b", found):
        return "word"
    return "substring"


def _pick_recipient(plan: TaskPlan, reply: str) -> str:
    """Interpret a short reply against the candidates we actually offered."""
    if not reply or reply.endswith("?"):
        return ""
    rows_offered = _candidate_rows(plan)
    if len(rows_offered) == 1 and _RETRY.match(reply):
        # "yes" against a single offered contact is an explicit selection.
        return str(rows_offered[0].get("name") or "")
    if _LITERAL_EMAIL.match(reply) or _LITERAL_PHONE.match(reply):
        return reply
    rows = _candidate_rows(plan)
    lowered = _norm(reply)
    if not rows:
        return reply if 0 < len(reply.split()) <= 4 else ""
    for row in rows:
        for handle in row.get("handles") or []:
            if _norm(handle) == lowered:
                return str(handle)
    named = [row for row in rows if _norm(row.get("name")) == lowered]
    if len(named) == 1:
        return str(named[0].get("name") or "")
    # "the gmail one" — every meaningful token must land in exactly one handle.
    tokens = [token for token in re.findall(r"[a-z0-9@.\-+_]+", lowered)
              if token not in _FILLER]
    if tokens:
        hits = [handle for row in rows for handle in row.get("handles") or []
                if all(token in _norm(handle) for token in tokens)]
        if len(dict.fromkeys(hits)) == 1:
            return str(hits[0])
        named = [row for row in rows
                 if all(token in _norm(row.get("name")) for token in tokens)]
        if len(named) == 1:
            return str(named[0].get("name") or "")
    return ""


def _resolve_recipient_slot(plan: TaskPlan, *, contacts_resolver) -> tuple[str, str]:
    """Ground an outbound recipient to one exact address before planning.

    Runs in the engine, between compile and plan, for the same reason
    `_resolve_operation_targets` does: the compiler has no store and cannot ask
    a question, and the executor is past the point where approval binds to
    step args.
    """
    # Deliberately NOT gated on `missing_slots` in general: an email still
    # missing its subject line should learn it cannot reach that person at all
    # before being asked for anything else.  Only an unknown recipient stops us.
    if (plan.intent not in OUTBOUND_INTENTS or plan.resolved_recipient
            or plan.recipient is None or "recipient" in plan.missing_slots):
        return "", ""
    want_email = str(plan.channel.value or "") == "email"
    raw = " ".join(str(plan.recipient.value or "").split()).strip()
    if not raw:
        return _ask_for_recipient(
            plan, "Who should I send that to?", "recipient_missing")

    if _SELF.match(raw):
        # The user's own address exists in the prompt for ATTRIBUTION. Filling
        # a recipient slot from it is the bug _own_address_guard and
        # builtin._wrong_account_path both exist to stop, so ask instead.
        return _ask_for_recipient(
            plan,
            "Which address should I use for you — "
            + ("what email address?" if want_email else "what number?"),
            "recipient_self_unspecified")

    if _LITERAL_EMAIL.match(raw):
        if not want_email:
            return _ask_for_recipient(
                plan, f"{raw} is an email address, and this is going by "
                "Messages. What number should I use, or should I send it as "
                "an email instead?", "recipient_channel_mismatch")
        plan.resolved_recipient = {
            "contact_id": "", "display_name": raw, "channel": "email",
            "address": raw, "kind": "email", "candidates_considered": 1,
            "source": "literal", "resolved_at": time.time()}
        return "", ""
    if _LITERAL_PHONE.match(raw):
        if want_email:
            # A phone number cannot receive an email. Never silently downgrade
            # the channel the user asked for.
            return _ask_for_recipient(
                plan, f"{raw} is a phone number, so I can’t send an email to "
                "it. What email address should I use?",
                "recipient_channel_mismatch")
        plan.resolved_recipient = {
            "contact_id": "", "display_name": raw, "channel": "messages",
            "address": raw, "kind": "phone", "candidates_considered": 1,
            "source": "literal", "resolved_at": time.time()}
        return "", ""

    matches = contacts_resolver(raw) or []
    if not matches:
        # No fuzzy matching on purpose: "trishe" is a question, not an
        # autocorrect. Sending to the wrong person is unrecoverable.
        return _ask_for_recipient(
            plan, f"I couldn’t find a saved contact matching “{raw}.” "
            "What address or number should I use?", "recipient_not_found",
            candidates=[])
    if len(matches) > 1:
        listed = ", ".join(str(row.get("name") or "") for row in matches[:6])
        return _ask_for_recipient(
            plan, f"“{raw}” matches several contacts: {listed}. "
            "Which one do you mean?", "recipient_ambiguous",
            candidates=[{"name": str(row.get("name") or ""),
                         "handles": list(row.get("handles") or [])}
                        for row in matches])

    contact = matches[0]
    name = str(contact.get("name") or raw)
    handles = [str(value) for value in contact.get("handles") or []]
    if _match_tier(raw, name) == "substring":
        # One result from a substring match is a suggestion, not a decision.
        return _ask_for_recipient(
            plan, f"I don’t have a contact called “{raw}.” Did you mean "
            f"{name}?", "recipient_needs_confirmation",
            candidates=[{"name": name, "handles": handles}])
    if want_email:
        emails = list(dict.fromkeys(h for h in handles if "@" in h))
        if not emails:
            return _ask_for_recipient(
                plan, f"{name} has no email address saved in Contacts — only a "
                "phone number. What email address should I use?",
                "recipient_no_email", candidates=[])
        if len(emails) > 1:
            return _ask_for_recipient(
                plan, f"{name} has several saved email addresses: "
                f"{', '.join(emails)}. Which one should I use?",
                "recipient_ambiguous",
                candidates=[{"name": name, "handles": emails}])
        address, kind = emails[0], "email"
    else:
        address = str(contact.get("preferred") or "")
        if not address:
            return _ask_for_recipient(
                plan, f"{name} has no usable number saved in Contacts. "
                "What number should I use?", "recipient_no_handle",
                candidates=[])
        kind = "email" if "@" in address else "phone"

    plan.resolved_recipient = {
        "contact_id": name, "display_name": name,
        "channel": str(plan.channel.value or ""), "address": address,
        "kind": kind, "candidates_considered": len(matches),
        # For messages a contact with several handles resolves to the one they
        # have demonstrably messaged from (_preferred_handle). That is a real
        # policy, not an accident: record what it chose between so the choice
        # is auditable, and the approval card shows the address it picked.
        "handles_considered": handles,
        "match_tier": _match_tier(raw, name),
        "source": "contacts", "resolved_at": time.time()}
    plan.parameters.pop("recipient_candidates", None)
    return "", ""


def _save(store, sid: str, plan: TaskPlan, event: str,
          payload: dict | None = None) -> None:
    store.save_workflow(sid, plan.to_dict())
    store.add_workflow_event(plan.id, event, payload)


def _turn(plan: TaskPlan, response: str = "", event: str = "",
          executable: bool = False, *, started: float) -> TaskTurn:
    return TaskTurn(plan, response, event, executable, {
        "engine_ms": round((time.perf_counter() - started) * 1000, 3),
        "compiler": "deterministic",
        "model_invocations": 0,
    })


def _question(plan: TaskPlan) -> str:
    if plan.intent in OUTBOUND_INTENTS:
        channel = "email" if plan.channel.value == "email" else "message"
        if "recipient" in plan.missing_slots:
            return f"Who should I send that {channel} to?"
        if "recipient.address" in plan.missing_slots:
            return ("What address should I use?" if plan.channel.value == "email"
                    else "What number should I use?")
        if "temporal.time" in plan.missing_slots:
            requested = str(plan.parameters.get(
                "schedule_requested", SlotValue()).value or "").strip()
            if requested:
                return (f"I couldn’t work out when “{requested}” is, so I "
                        f"haven’t sent anything. When should I send that "
                        f"{channel}?")
            return f"When should I send that {channel}?"
        if "email.subject" in plan.missing_slots:
            return "What subject line should I use?"
        if "subject" in plan.missing_slots:
            return f"What should the {channel} say?"
        return f"I haven’t sent anything yet. What should the {channel} say?"
    if "scope" in plan.missing_slots:
        return ("Which reminders should I delete: today, tomorrow, past due, "
                "upcoming, or all?")
    if "target" in plan.missing_slots:
        if plan.intent == "reminder.complete":
            return "Which reminder should I mark done?"
        if plan.intent == "reminder.update":
            return "Which reminder should I update?"
        return "Which reminder do you mean?"
    if "update.change" in plan.missing_slots:
        requested = plan.parameters.get("requested_day")
        if requested and requested.value == "today":
            return ("That reminder’s original time has already passed today. "
                    "What time today should I use?")
        return "What should I change about that reminder?"
    if "subject" in plan.missing_slots:
        return "What should I remind you about?"
    if "temporal.lead_time" in plan.missing_slots:
        reference = re.sub(r"^my\b", "your", plan.temporal.reference, flags=re.I)
        return ("I haven’t created a reminder yet. How long before "
                f"{reference} should I remind you?")
    return "I haven’t created a reminder yet. When should I remind you?"


def _norm(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _scope_bounds(scope: str, now: datetime) -> tuple[float | None, float | None]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if scope == "today":
        return today.timestamp(), (today + timedelta(days=1)).timestamp()
    if scope == "tomorrow":
        return (today + timedelta(days=1)).timestamp(), (today + timedelta(days=2)).timestamp()
    if scope == "past_due":
        return None, now.timestamp()
    if scope == "upcoming":
        return now.timestamp(), None
    return None, None


def _reminder_candidates(assistant_store, *, scope: str = "all",
                         now: datetime) -> list[dict]:
    start, end = _scope_bounds(scope, now)
    rows = assistant_store.active_between(start, end)
    return [row for row in rows
            if row.get("source") in _REMINDER_SOURCES
            or bool(_REMINDER_SOURCES & set(row.get("duplicate_sources") or []))]


def _snapshot(row: dict) -> dict:
    return {
        "id": str(row.get("id") or ""),
        "duplicate_ids": [str(value) for value in row.get("duplicate_ids") or []],
        "title": str(row.get("title") or ""),
        "when_ts": float(row.get("when_ts") or 0),
        "source": str(row.get("source") or ""),
    }


def _resolve_operation_targets(plan: TaskPlan, assistant_store, *,
                               now: datetime) -> tuple[str, str]:
    """Ground a typed operation to reminder-only rows; never guess a target."""
    if (not plan.intent.startswith("reminder.")
            or plan.intent == "reminder.create" or plan.missing_slots):
        return "", ""
    scope = str(plan.parameters.get("scope", SlotValue("all")).value or "all")
    rows = _reminder_candidates(assistant_store, scope=scope, now=now)
    if plan.intent == "reminder.complete":
        horizon = now.timestamp() + 365 * 86400
        rows = [row for row in rows
                if now.timestamp() - 300 <= float(row.get("when_ts") or 0) <= horizon]
    target = _norm(plan.target.value)
    if target:
        exact = [row for row in rows if _norm(row.get("title")) == target]
        rows = exact or [row for row in rows if target in _norm(row.get("title"))]

    if not rows:
        if plan.intent == "reminder.delete" and not target:
            plan.status = "completed"
            plan.resolved_targets = []
            return (f"No active reminders were found for {scope.replace('_', ' ')}. "
                    "Nothing was deleted."), "no_match"
        plan.target = SlotValue()
        plan.resolved_targets = []
        plan.missing_slots = ["target"]
        plan.status = "waiting_for_input"
        return (f"I couldn’t find an active reminder matching “{target}.” "
                f"{_question(plan)}"), "target_not_found"

    singular = plan.intent in {"reminder.update", "reminder.complete"} or bool(target)
    if singular and len(rows) != 1:
        labels = "; ".join(
            f"{row.get('title')} ({datetime.fromtimestamp(float(row['when_ts'])):%a %b %-d at %-I:%M %p})"
            for row in rows[:5])
        plan.target = SlotValue()
        plan.resolved_targets = []
        plan.missing_slots = ["target"]
        plan.status = "waiting_for_input"
        return f"I found several matching reminders: {labels}. {_question(plan)}", "target_ambiguous"

    plan.resolved_targets = [_snapshot(row) for row in rows]
    if plan.intent == "reminder.update" and len(rows) == 1:
        day = plan.parameters.get("day")
        if day and day.value == "today" and not plan.temporal.absolute_iso:
            old = datetime.fromtimestamp(float(rows[0].get("when_ts") or 0))
            proposed = now.replace(hour=old.hour, minute=old.minute,
                                   second=0, microsecond=0)
            if proposed.timestamp() < now.timestamp() - 60:
                plan.parameters["requested_day"] = SlotValue(
                    "today", day.source, day.turn, day.confidence, day.original)
                del plan.parameters["day"]
                plan.recompute_status()
                return _question(plan), "past_time_clarification"
    return "", ""


def _contextual_update(store, sid: str, prompt: str, *, now: datetime,
                       persist: bool) -> TaskPlan | None:
    match = _CONTEXT_DAY_CORRECTION.match(prompt)
    clock = re.fullmatch(
        r"\s*(?:(?:actually|like)\s+)?"
        r"(\d{1,2}(?::\d{2})?\s*[ap]m|noon|midnight)"
        r"(?:\s+(?:today|tomorrow))?[.!]?\s*", prompt, re.I)
    if (not match and not clock) or not persist:
        return None
    raw = store.latest_task(sid)
    if not raw:
        return None
    prior = TaskPlan.from_dict(raw)
    if prior.status != "completed":
        return None
    target = (str(prior.subject.value or "") if prior.intent == "reminder.create"
              else str(prior.target.value or ""))
    if not target and prior.resolved_targets:
        target = str(prior.resolved_targets[0].get("title") or "")
    if not target:
        return None
    plan = TaskPlan(
        kind="task.reminder.update", intent="reminder.update",
        original_request=prompt,
        target=SlotValue(target, "context", original=target),
        temporal=TemporalValue(original=prompt, timezone=prior.temporal.timezone),
        parameters=({"day": SlotValue(match.group("day").casefold(), "explicit",
                                      original=match.group("day"))} if match else {}),
    )
    if clock:
        if not prior.temporal.absolute_iso:
            return None
        base = datetime.fromisoformat(prior.temporal.absolute_iso)
        resolved, _ = resolve_named_time(clock.group(1), now=base)
        if resolved is None:
            return None
        target_time = base.replace(hour=resolved.hour, minute=resolved.minute)
        if target_time.timestamp() <= now.timestamp():
            return None
        plan.temporal.absolute_iso = target_time.isoformat(timespec="minutes")
        plan.temporal.source = "followup"
    plan.recompute_status()
    return plan


def _resolve_reference_time(plan: TaskPlan, assistant_store, *, now: datetime) -> str:
    candidates = resolve_event_reference(
        plan.temporal.reference,
        assistant_store.upcoming(now=now.timestamp(), days=180))
    if not candidates:
        return (f"I couldn’t find an upcoming calendar event matching "
                f"“{plan.temporal.reference}.” I haven’t created the reminder.")
    if len(candidates) > 1:
        labels = "; ".join(
            f"{item.get('title')} ({datetime.fromtimestamp(float(item['when_ts'])):%a %b %-d at %-I:%M %p})"
            for item in candidates[:5])
        return f"I found several matching events: {labels}. Which one do you mean?"
    event = candidates[0]
    event_when = datetime.fromtimestamp(float(event["when_ts"]))
    target = apply_lead(event_when, int(plan.temporal.lead_seconds or 0))
    if target.timestamp() < now.timestamp() - 60:
        return (f"That reminder time would already have passed ({target:%a %b %-d at %-I:%M %p}). "
                "I haven’t created it. What time should I use instead?")
    plan.temporal.reference_id = str(event.get("source_id") or event.get("id") or "")
    plan.temporal.reference_when_iso = event_when.isoformat(timespec="minutes")
    plan.temporal.absolute_iso = target.isoformat(timespec="minutes")
    plan.temporal.source = "resolved"
    return ""


def prepare_task_turn(store, sid: str, prompt: str, *, assistant_store,
                      persist: bool = True, now: datetime | None = None,
                      contacts_resolver=None, mail_reader=None) -> TaskTurn | None:
    """Create or advance one typed task without consulting a model."""
    started = time.perf_counter()
    now = now or datetime.now()
    contacts_resolver = contacts_resolver or _default_contacts_resolver
    active_raw = store.active_task(sid) if persist else None
    active = TaskPlan.from_dict(active_raw) if active_raw else None
    new_plan = compile_task(prompt, now=now)
    if ((new_plan and new_plan.intent == "email.reply")
            or (new_plan is None and active and active.intent == "email.reply")):
        from service.tasks.reply_engine import prepare_reply_turn
        from service.tasks.source_readers import current_mail_reader
        return prepare_reply_turn(store, sid, prompt, new_plan, active,
                                  reader=mail_reader or current_mail_reader(),
                                  now=now, persist=persist)

    from service.tasks.outbound_language import answer_language_question, language_question
    if new_plan and language_question(new_plan):
        if active and persist and not active.claimed_calls:
            old_status = active.status
            active.status = "superseded"
            if store.transition_task(sid, active.to_dict(), from_status=old_status):
                store.add_workflow_event(active.id, "superseded", {"by": new_plan.id})
        if persist:
            _save(store, sid, new_plan, "language_clarification", {})
        return _turn(new_plan, language_question(new_plan), "language_clarification", started=started)
    if new_plan is None and active and language_question(active) and not _CANCEL.match(prompt):
        if answer_language_question(active, prompt, now=now):
            active.revision += 1
            active.recompute_status()
            new_plan, active = active, None
        elif _UNRELATED_SUBJECT_REPLY.search(prompt) and prompt.strip().casefold() != "send then":
            return None
        else:
            return _turn(active, language_question(active), "language_clarification", started=started)

    # A pending reminder clarification must not consume an explicit request in
    # another domain.  This happened after an ambiguous "12pm" correction:
    # "check my email for purchases from PlayStation" was interpreted as the
    # title of the reminder to update, and the mail read never reached routing.
    # Leave the reminder pending (the user may return to it later), but let the
    # downstream workflow/read compilers handle this turn.
    unrelated_prompt = prompt.strip().strip("*_` ")
    # This guard exists so a pending clarification cannot swallow a genuine new
    # request. But it keys off leading verbs, and the natural way to answer
    # "which address?" starts with one of them ("email the work one"), so an
    # answer to the question we just asked was being dropped as unrelated.
    # Scope it: text that resolves against the candidates we actually OFFERED
    # is an answer, whatever verb it starts with.
    answers_open_slot = (
        active is not None
        and bool(_candidate_rows(active))
        and bool(_pick_recipient(active, unrelated_prompt.strip(" ."))))
    if (new_plan is None and active is not None
            and active.status in {"waiting_for_input", "failed"}
            and _UNRELATED_SUBJECT_REPLY.search(unrelated_prompt)
            and not _channel_correction(active, unrelated_prompt)
            and not answers_open_slot):
        return None

    if new_plan is None and active is None:
        new_plan = _contextual_update(
            store, sid, prompt, now=now, persist=persist)

    # A denied send is terminal. `active_task` already drops it, but a bare
    # "yes" would then fall through to the router with no plan attached — the
    # shape that produces an unrequested second send. Answer it explicitly.
    if new_plan is None and active is None and persist and _RETRY.match(prompt):
        raw_latest = store.latest_task(sid)
        latest = TaskPlan.from_dict(raw_latest) if raw_latest else None
        if (latest is not None and latest.intent in OUTBOUND_INTENTS
                and latest.status in {"denied", "cancelled"}
                and time.time() - float(latest.updated_at or 0) <= 900):
            return _turn(
                latest, ("That request is closed, but sending was already attempted. "
                         "Check its outcome before explicitly requesting another send."
                         if latest.claimed_calls else
                         "That send is closed — nothing was sent. Tell me who "
                         "to send it to and what to say if you want to try again."),
                "closed_send_followup", started=started)

    if new_plan is not None:
        if active is not None and not active.claimed_calls:
            old_status = active.status
            active.status = "superseded"
            active.updated_at = time.time()
            if persist:
                if store.transition_task(sid, active.to_dict(), from_status=old_status):
                    store.add_workflow_event(active.id, "superseded", {"by": new_plan.id})
        plan = new_plan
        event = "task_compiled"
    elif active is None:
        return None
    else:
        plan = active
        if _CANCEL.match(prompt):
            if plan.claimed_calls:
                return _turn(plan, "That action was already attempted, so I can’t confirm cancellation. "
                             "Check its outcome; I won’t retry automatically.",
                             "cancellation_too_late", started=started)
            old_status = plan.status
            plan.status = "cancelled"
            plan.updated_at = time.time()
            if persist:
                if not store.transition_task(sid, plan.to_dict(), from_status=old_status):
                    return _turn(plan, "That request changed or its action already started. "
                                 "Check its current outcome before trying again.",
                                 "stale_cancellation", started=started)
                store.add_workflow_event(plan.id, "cancelled", {"reply": prompt})
            noun = ("send" if plan.intent in OUTBOUND_INTENTS
                    else "reminder request")
            return _turn(plan, f"Okay, I cancelled that {noun}.", "cancelled",
                         started=started)
        if plan.status == "running":
            if _RETRY.match(prompt) or compile_task(prompt, now=now):
                return _turn(
                    plan,
                    ("That send is already running. I won’t send it twice."
                     if plan.intent in OUTBOUND_INTENTS else
                     "That reminder request is already running. I won’t create a duplicate."),
                    "duplicate_blocked", started=started)
            return None
        if plan.status not in {"waiting_for_input", "failed"}:
            return None

        if (plan.intent in {"reminder.create", "reminder.update"}
                and {"temporal.time", "temporal.lead_time", "update.change"}
                .intersection(plan.missing_slots)
                and is_unsupported_time_answer(prompt)):
            # Preserve target/subject/source evidence and the persisted plan.
            # In particular, don't consume this as a missing title or resolve
            # only its "tomorrow" fragment into an executable 09:00 plan.
            return _turn(plan, "What exact time should I use for the reminder? "
                         "Please give a clock time such as 6:30 am tomorrow.",
                         "clock_clarification", started=started)

        changed = False
        channel_corrected = False
        if plan.intent in OUTBOUND_INTENTS:
            reply = prompt.strip(" .")
            if channel := _channel_correction(plan, prompt):
                # Retarget the channel without losing the body or the
                # recipient the user already gave: only the address snapshot
                # is invalidated.
                plan.channel = SlotValue(channel, "correction", original=prompt)
                plan.intent = ("email.send" if channel == "email"
                               else "message.send")
                plan.kind = f"task.{plan.intent}"
                plan.resolved_recipient = None
                plan.parameters.pop("recipient_candidates", None)
                changed = channel_corrected = True
            if not channel_corrected and (
                    "recipient" in plan.missing_slots
                    or "recipient.address" in plan.missing_slots):
                if picked := _pick_recipient(plan, reply):
                    plan.recipient = SlotValue(picked, "followup", turn=plan.revision,
                                               original=prompt)
                    plan.resolved_recipient = None
                    plan.parameters.pop("recipient_candidates", None)
                    changed = True
            if ("email.subject" in plan.missing_slots and not channel_corrected
                    and reply and len(reply.split()) <= 20
                    and not reply.endswith("?")):
                plan.parameters["email_subject"] = SlotValue(
                    reply, "followup", original=prompt)
                changed = True
        if "scope" in plan.missing_slots:
            lowered = prompt.casefold()
            if re.search(r"\b(?:all|every)\b", lowered):
                scope = "all"
            elif re.search(r"\b(?:past[ -]?due|overdue|old)\b", lowered):
                scope = "past_due"
            elif re.search(r"\btomorrow\b", lowered):
                scope = "tomorrow"
            elif re.search(r"\b(?:upcoming|future)\b", lowered):
                scope = "upcoming"
            elif re.search(r"\btoday\b", lowered):
                scope = "today"
            else:
                scope = ""
            if scope:
                plan.parameters["scope"] = SlotValue(
                    scope, "followup", original=prompt)
                changed = True
        if "target" in plan.missing_slots:
            candidate = prompt.strip(" .")
            if (candidate and len(candidate.split()) <= 30
                    and not candidate.endswith("?")
                    and not _UNRELATED_SUBJECT_REPLY.search(candidate)):
                plan.target.value = candidate
                plan.target.source = "followup"
                plan.target.original = prompt
                plan.resolved_targets = []
                changed = True
        if "subject" in plan.missing_slots and not changed:
            candidate = prompt.strip(" .")
            if (candidate and len(candidate.split()) <= 30
                    and not candidate.endswith("?")
                    and not _UNRELATED_SUBJECT_REPLY.search(candidate)):
                plan.subject.value = candidate
                plan.subject.source = "followup"
                plan.subject.original = prompt
                changed = True
        if "update.change" in plan.missing_slots:
            candidate = prompt.strip(" .")
            resolved, defaulted = resolve_named_time(candidate, now=now)
            if re.fullmatch(r"today|tomorrow", candidate, re.I):
                plan.parameters["day"] = SlotValue(
                    candidate.casefold(), "followup", original=prompt)
                changed = True
            elif resolved is not None:
                plan.temporal.absolute_iso = resolved.isoformat(timespec="minutes")
                plan.temporal.original = prompt
                plan.temporal.source = "followup"
                plan.temporal.defaulted_part_of_day = defaulted
                changed = True
        if "temporal.reference" in plan.missing_slots:
            candidate = prompt.strip(" .")
            if (candidate and len(candidate.split()) <= 20
                    and not candidate.endswith("?")):
                plan.temporal.reference = candidate
                plan.temporal.reference_id = ""
                plan.temporal.reference_when_iso = ""
                plan.temporal.original = prompt
                plan.temporal.source = "followup"
                changed = True
        if ("temporal.time" in plan.missing_slots
                or "temporal.lead_time" in plan.missing_slots):
            resolved, defaulted = resolve_named_time(prompt, now=now)
            lead = parse_lead_seconds(prompt) if plan.temporal.reference else None
            if resolved is not None:
                plan.temporal.absolute_iso = resolved.isoformat(timespec="minutes")
                plan.temporal.reference = ""
                plan.temporal.reference_id = ""
                plan.temporal.lead_seconds = None
                plan.temporal.original = prompt
                plan.temporal.source = "followup"
                plan.temporal.defaulted_part_of_day = defaulted
                changed = True
            elif lead is not None:
                plan.temporal.lead_seconds = lead
                plan.temporal.original = prompt
                plan.temporal.source = "followup"
                changed = True
        if not changed:
            if _CORRECTION_WITHOUT_TIME.search(prompt):
                return _turn(plan, _question(plan), "clarification_repeated",
                             started=started)
            return None
        plan.revision += 1
        plan.idempotency_key = __import__("uuid").uuid4().hex
        plan.recompute_status()
        event = "task_updated"

    if (plan.intent == "reminder.create" and plan.temporal.absolute_iso
            and datetime.fromisoformat(plan.temporal.absolute_iso).timestamp() <= now.timestamp()):
        plan.temporal.absolute_iso = ""
        plan.recompute_status()
        if persist:
            _save(store, sid, plan, "past_time_clarification", {})
        return _turn(plan, "That reminder time has passed. Nothing was added. What future time should I use?",
                     "past_time_clarification", started=started)

    if (plan.intent in OUTBOUND_INTENTS and plan.temporal.absolute_iso
            and datetime.fromisoformat(plan.temporal.absolute_iso).timestamp()
            <= now.timestamp()):
        # Never silently downgrade a late scheduled send into an immediate one:
        # "at 8am" said at 9am must ask, not fire now.
        plan.temporal.absolute_iso = ""
        plan.missing_slots = ["temporal.time"]
        plan.status = "waiting_for_input"
        if persist:
            _save(store, sid, plan, "past_send_time", {})
        return _turn(
            plan, "That send time has already passed, so I haven’t sent "
            "anything. What time should I use instead?",
            "past_send_time", started=started)

    if plan.temporal.reference and plan.temporal.lead_seconds is not None:
        problem = _resolve_reference_time(plan, assistant_store, now=now)
        if problem:
            plan.status = "waiting_for_input"
            plan.missing_slots = ["temporal.reference"]
            if persist:
                _save(store, sid, plan, "reference_unresolved", {"message": problem})
            return _turn(plan, problem, "reference_unresolved", started=started)
        plan.recompute_status()

    problem, resolution_event = _resolve_operation_targets(
        plan, assistant_store, now=now)
    if not problem:
        problem, resolution_event = _resolve_recipient_slot(
            plan, contacts_resolver=contacts_resolver)
    if problem:
        if persist:
            _save(store, sid, plan, resolution_event, {
                "message": problem, "missing_slots": plan.missing_slots})
        return _turn(plan, problem, resolution_event, started=started)

    if plan.status == "waiting_for_input":
        if persist:
            _save(store, sid, plan, event, {"missing_slots": plan.missing_slots})
        return _turn(plan, _question(plan), event, started=started)

    try:
        plan_task(plan)
    except InvalidTaskPlan as exc:
        plan.status = "failed"
        plan.last_error = str(exc)
        if persist:
            _save(store, sid, plan, "validation_failed", {"error": str(exc)})
        return _turn(
            plan,
            ("I couldn’t safely construct that message, so nothing was sent."
             if plan.intent in OUTBOUND_INTENTS else
             "I couldn’t safely construct that reminder, so nothing was created."),
            "validation_failed", started=started)

    plan.status = "running"
    plan.updated_at = time.time()
    if persist:
        _save(store, sid, plan, "execution_started", {
            "revision": plan.revision,
            "steps": [step.__dict__ for step in plan.steps],
        })
    return _turn(plan, event="execution_started", executable=True, started=started)


def finish_task(store, sid: str, plan: TaskPlan, *, status: str,
                result: str = "") -> None:
    plan.status = status
    plan.last_error = result[:500] if status == "failed" else ""
    plan.updated_at = time.time()
    if store.transition_task(sid, plan.to_dict(), from_status="running"):
        store.add_workflow_event(plan.id, status, {"result": result[:500], "revision": plan.revision})
