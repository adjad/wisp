"""Persistent reply clarification and native preparation, before effect planning."""
from __future__ import annotations

from datetime import datetime
import re
import time

from service.tasks.models import SlotValue, TaskPlan, TaskTurn
from service.tasks.references import SourceRef, resolve_reference, select_candidate


_SCHEDULE_CLARIFICATION_ANSWERS = {"send then", "when to send", "delivery time"}


def _scheduled_reply_preflight(compiled: TaskPlan | None, active: dict | None,
                               prompt: str) -> bool:
    """Return true when this turn can only report unsupported reply scheduling.

    This check deliberately uses typed state only. It runs before native Mail
    warm-up or reader construction, so a known limitation cannot cause a
    source read as a side effect of explaining that limitation.
    """
    plan = compiled
    if plan is None and active and active.get("intent") == "email.reply":
        plan = TaskPlan.from_dict(active)
    if plan is None or plan.intent != "email.reply":
        return False
    if "reply.schedule" in plan.missing_slots:
        return True
    answer = prompt.strip().rstrip(".! ").casefold()
    return bool(plan.parameters.get("time_clarification")) and (
        answer in _SCHEDULE_CLARIFICATION_ANSWERS)


def mail_reference(text: str) -> SourceRef:
    value = text.strip(" .")
    hints: dict[str, str] = {}
    if match := re.search(r'\s+(?:in|on)\s+(?:the\s+)?["\u201c]?(.+?)["\u201d]?\s+account\b', value, re.I):
        hints["account"] = match.group(1).strip('"\u201c\u201d ')
        value = value[:match.start()] + value[match.end():]
    if match := re.search(r"\b(today|yesterday)\b", value, re.I):
        hints["day"] = match.group(1).lower()
        value = value[:match.start()] + value[match.end():]
    if match := re.search(r"\babout\s+(.+)$", value, re.I):
        hints["topic"] = match.group(1).strip(' "')
        value = value[:match.start()]
    if match := re.search(r"\bfrom\s+(.+)$", value, re.I):
        hints["sender"] = match.group(1).strip()
    elif match := re.match(r"(.+?)[’']s\s+(?:email|mail)\b", value, re.I):
        hints["sender"] = match.group(1).strip()
    elif not re.fullmatch(r"\s*(?:(?:that|the|this|an?)\s+)?(?:e-?mail)?\s*", value, re.I):
        hints["sender"] = value.strip()
    return SourceRef("email", hints=hints)


def prepare_reply_turn(store, sid: str, prompt: str, new: TaskPlan | None,
                       active: TaskPlan | None, *, reader, now: datetime,
                       persist: bool) -> TaskTurn | None:
    from service.tasks.engine import _CANCEL, _RETRY, _UNRELATED_SUBJECT_REPLY
    plan = new or active
    assert plan is not None
    previous_status, previous_revision = plan.status, plan.revision
    previous_claims = list(plan.claimed_calls)
    from service.tasks.outbound_language import (
        answer_language_question, language_question, mark_body_ambiguity,
    )

    def done(response: str, event: str) -> TaskTurn:
        plan.updated_at = time.time()
        if persist:
            if new:
                store.save_workflow(sid, plan.to_dict())
            elif not store.transition_task(
                    sid, plan.to_dict(), from_status=previous_status,
                    expected_revision=previous_revision, expected_claimed_calls=previous_claims):
                return TaskTurn(plan, "That reply request changed or sending already started. "
                                "Check its current outcome before trying again.", "stale_reply_turn")
            store.add_workflow_event(plan.id, event, {"revision": plan.revision})
        return TaskTurn(plan, response, event)

    if new:
        if active and persist and not active.claimed_calls:
            old_status = active.status
            active.status = "superseded"
            store.transition_task(sid, active.to_dict(), from_status=old_status)
    else:
        if _CANCEL.match(prompt):
            if plan.claimed_calls:
                return TaskTurn(plan, "Sending was already attempted, so I can’t confirm cancellation. "
                                "Check Mail for the outcome; I won’t retry automatically.", "cancellation_too_late")
            plan.status = "cancelled"
            return done("Okay, I cancelled that reply. Nothing was sent.", "cancelled")
        offered = plan.parameters.get("source_candidates", SlotValue([])).value or []
        picked = select_candidate(offered, prompt)
        body_edit = re.match(r"^(?:actually\s+)?(?:say|saying|make\s+it\s+say)\s+(.+)$", prompt, re.I | re.S)
        language_answer = False
        if language_question(plan):
            language_answer = answer_language_question(plan, prompt, now=now)
            if not language_answer:
                answer = prompt.strip().rstrip(".! ").casefold()
                if (answer in _SCHEDULE_CLARIFICATION_ANSWERS
                        and plan.parameters.get("time_clarification")):
                    return done("Scheduling an email reply isn’t supported yet. Nothing was sent.",
                                "reply_schedule_unsupported")
                if _UNRELATED_SUBJECT_REPLY.search(prompt):
                    return None
                return done(language_question(plan), "language_clarification")
        pending_correction = (plan.status == "running" and not plan.claimed_calls
                              and (picked or body_edit or prompt.strip().casefold() == "reply all"))
        if plan.status in {"running", "failed"} and not pending_correction:
            if _RETRY.match(prompt) or picked or body_edit or prompt.strip().casefold() == "reply all":
                return done("That reply was already attempted or is still running. Check Mail before explicitly requesting another reply.", "duplicate_blocked")
            return None
        if language_answer:
            pass
        elif picked:
            if not picked.actionable:
                return done("I can see that email, but it has no actionable message ID yet. Refresh Mail first.", "source_not_actionable")
            plan.resolved_references["reply.target"] = picked.to_dict()
        elif body_edit:
            from service.tasks.compiler import _clean_body
            plan.subject = SlotValue(_clean_body(body_edit.group(1)), "correction", original=prompt)
            mark_body_ambiguity(plan, body_edit.group(1))
        elif prompt.strip().casefold() == "reply all":
            plan.parameters["reply_all"] = SlotValue(True, "correction")
        elif _RETRY.match(prompt):
            pass  # bounded source refresh, never retries an attempted effect
        elif _UNRELATED_SUBJECT_REPLY.search(prompt):
            return None
        elif "subject" in plan.missing_slots and plan.resolved_references.get("reply.target"):
            from service.tasks.compiler import _clean_body
            plan.subject = SlotValue(_clean_body(prompt), "followup", original=prompt)
            mark_body_ambiguity(plan, prompt)
        elif (re.match(r"^(?:from|about|in|on|the|that|this|today|yesterday)\b", prompt, re.I)
              or re.search(r"\bemail\b", prompt, re.I)
              or (not offered and "reply.target" in plan.missing_slots and len(prompt.split()) <= 4)):
            old = plan.parameters.get("reference_hints", SlotValue(mail_reference(str(plan.target.value or "")).hints)).value
            hints = {**old, **mail_reference(prompt).hints}
            plan.parameters["reference_hints"] = SlotValue(hints, "followup", original=prompt)
            plan.target = SlotValue(prompt.strip(), "followup", original=prompt)
            plan.resolved_references.pop("reply.target", None)
        else:
            return done("Choose one of the offered emails, or narrow its sender, topic or account.", "source_selection_needed")
        plan.revision += 1
        plan.parameters.pop("reply_args", None)

    plan.recompute_status()
    if "reply.schedule" in plan.missing_slots:
        return done("Scheduling an email reply isn’t supported yet. Nothing was sent.", "reply_schedule_unsupported")
    if question := language_question(plan):
        return done(question, "language_clarification")
    if not plan.resolved_references.get("reply.target"):
        if reader is None:
            from service.tasks.source_readers import current_mail_reader
            reader = current_mail_reader()
        ref = mail_reference(str(plan.target.value or ""))
        if saved := plan.parameters.get("reference_hints"):
            ref.hints = dict(saved.value)
        if not ref.hints:
            return done("Which email should I reply to? Give its sender, subject or account.", "source_reference_needed")
        result = resolve_reference(ref, reader, now=now)
        plan.parameters["source_candidates"] = SlotValue([c.to_dict() for c in result.candidates], "resolved")
        plan.parameters["source_coverage"] = SlotValue({"scope": result.scope,
                                                       "synced_at": result.synced_at,
                                                       "total_matches": result.total_matches}, "resolved")
        if result.status != "resolved":
            return done(result.question, "source_" + result.status)
        plan.resolved_references["reply.target"] = result.snapshot
    plan.recompute_status()
    if "subject" in plan.missing_slots:
        return done("What should the reply say?", "reply_body_needed")
    return done("", "reply_prepare")


async def prepare_task_turn_async(store, sid: str, prompt: str, *, assistant_store,
                                  persist: bool = True, now: datetime | None = None,
                                  mail_reader=None, reply_preparer=None, allow_native: bool = True,
                                  contacts_resolver=None):
    from service.tasks.compiler import compile_task
    from service.tasks.engine import prepare_task_turn
    from service.tasks.planner import plan_task
    from service.tools.action_tools import prepare_reply_args
    now = now or datetime.now()
    compiled = compile_task(prompt, now=now)
    active = store.active_task(sid) if persist else None
    needs_mail = ((compiled and compiled.intent == "email.reply") or
                  (compiled is None and active and active.get("intent") == "email.reply"))
    from service.tasks.engine import _CANCEL, _UNRELATED_SUBJECT_REPLY
    unrelated = compiled is None and _UNRELATED_SUBJECT_REPLY.search(prompt)
    scheduled_reply = _scheduled_reply_preflight(compiled, active, prompt)
    should_warm = (needs_mail and not scheduled_reply
                   and not unrelated and not _CANCEL.match(prompt))
    if should_warm and mail_reader is None and allow_native:
        from service.tools.email_tools import ensure_reply_source
        await ensure_reply_source()
    turn = prepare_task_turn(store, sid, prompt, assistant_store=assistant_store,
                             now=now, persist=persist, contacts_resolver=contacts_resolver,
                             mail_reader=mail_reader)
    if not turn or turn.event != "reply_prepare":
        return turn
    plan = turn.plan
    if not allow_native:
        turn.response = "Dry run: the selected email needs a native reply preview. Nothing was prepared or sent."
        return turn
    source = plan.resolved_references["reply.target"]["fields"]
    args, problem = await (reply_preparer or prepare_reply_args)({
        "message_id": source["message_id"], "account": source["account"],
        **({"account_id": source["account_id"]} if source.get("account_id") else {}),
        "body": str(plan.subject.value),
        "reply_all": bool(plan.parameters["reply_all"].value),
    })
    if args is None:
        turn.response, turn.event = problem, "reply_unavailable"
    else:
        plan.parameters["reply_args"] = SlotValue(args, "resolved", turn=plan.revision)
        plan.recompute_status()
        plan_task(plan)
        plan.status = "running"
        turn.executable, turn.event = True, "execution_started"
    if persist:
        if store.transition_task(sid, plan.to_dict(), from_status="waiting_for_input"):
            store.add_workflow_event(plan.id, turn.event, {"revision": plan.revision})
        else:
            turn.executable = False
            turn.response = "That reply request changed while Mail was preparing it. Nothing was sent by this attempt."
            turn.event = "stale_preparation"
    return turn
