"""Persistent reply clarification and native preparation, before effect planning."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import time

from service.tasks.models import SlotValue, TaskPlan, TaskTurn
from service.tasks.references import SourceRef, resolve_reference, select_candidate


_SCHEDULE_CLARIFICATION_ANSWERS = {"send then", "when to send", "delivery time"}


def _reference_correction(prompt: str) -> bool:
    return bool(re.match(r"^(?:from|about|in|on|the|that|this|today|yesterday|tomorrow|tonight|at|by|next|later)\b", prompt, re.I)
                or re.search(r"\bemail\b", prompt, re.I))


def _reference_turn(plan: TaskPlan, prompt: str) -> bool:
    offered = plan.parameters.get("source_candidates", SlotValue([])).value or []
    return (_reference_correction(prompt)
            or (not offered and "reply.target" in plan.missing_slots and len(prompt.split()) <= 4))


@dataclass(frozen=True)
class _ReferenceUpdate:
    hints: dict[str, str]
    schedule_requested: str
    error: str
    uncertain: bool


def _reference_update(plan: TaskPlan, prompt: str) -> _ReferenceUpdate:
    """Accumulate intent before deciding whether the corrected source is usable.

    Valid supplied fields replace only those fields. An invalid account clears
    the old account, not new sender/topic/day or delivery intent. Unassignable
    fragments keep a persisted restatement requirement; later partial fixes
    cannot revive the older source. This pure decision also drives preflight.
    """
    from service.tasks.reply_parser import (
        complete_reply_selector, delivery_before_invalid_account,
        parse_reply_reference, recover_reply_reference,
    )
    old = plan.parameters.get("reference_hints")
    uncertain = bool(plan.parameters.get("reply_reference_uncertain", SlotValue(False)).value)
    needs_account = bool(plan.parameters.get("reply_selector_error"))
    when = str(plan.parameters.get("schedule_requested", SlotValue("")).value or "")
    if old is not None:
        hints = dict(old.value)
    else:
        original = parse_reply_reference(str(plan.target.value or ""))
        recovered = recover_reply_reference(original.reference) if original.selector_error else original
        uncertain = original.selector_error != "" and recovered is None
        hints = recovered.hints if recovered is not None else ({"day": original.day} if original.day else {})
        if recovered is not None:
            when = recovered.schedule_requested or when

    correction = parse_reply_reference(prompt)
    if correction.selector_error:
        # Recover both source and delivery fields from outside bounded account
        # clauses. Quoted temporal account names never become delivery timing.
        recovered = recover_reply_reference(prompt)
        hints.pop("account", None)
        if recovered is None:
            uncertain = True
            when = delivery_before_invalid_account(prompt) or when
        else:
            hints.update(recovered.hints)
            when = recovered.schedule_requested or when
        return _ReferenceUpdate(hints, when, correction.selector_error, uncertain)

    hints.update(correction.hints)
    when = correction.schedule_requested or when
    if uncertain and complete_reply_selector(correction):
        uncertain = False
    error = ""
    if uncertain:
        error = ("I can’t safely recover the latest email selectors. Restate the complete "
                 "email selector, including sender, topic, source day if any, and account.")
    elif needs_account and not hints.get("account"):
        error = "Specify the complete corrected Mail account."
    return _ReferenceUpdate(hints, when, error, uncertain)


def _reply_stops_before_mail(compiled: TaskPlan | None, active: dict | None,
                             prompt: str, *, now: datetime) -> bool:
    """Return true when typed reply handling will stop before source resolution.

    This check deliberately uses typed state only. It runs before native Mail
    warm-up or reader construction, so a scheduling limitation or wording
    clarification cannot read Mail merely to explain why the turn cannot yet
    prepare a reply.
    """
    plan = compiled
    if plan is None and active and active.get("intent") == "email.reply":
        plan = TaskPlan.from_dict(active)
    if plan is None or plan.intent != "email.reply":
        return False
    from service.tasks.outbound_language import answer_language_question, language_question
    if "reply.schedule" in plan.missing_slots:
        return True
    if compiled is None and _reference_turn(plan, prompt):
        update = _reference_update(plan, prompt)
        if update.error or update.schedule_requested:
            return True
        if plan.parameters.get("reply_selector_error"):
            return bool(language_question(plan))  # Other unresolved fields still stop Mail.
    if plan.parameters.get("reply_selector_error"):
        return True
    if not language_question(plan):
        return False
    if compiled is not None:
        return True
    # Probe a detached copy: unresolved clarification answers stop before Mail;
    # answers that make the literal body usable may continue to source lookup.
    probe = TaskPlan.from_dict(plan.to_dict())
    return not answer_language_question(probe, prompt, now=now)


def mail_reference(text: str) -> SourceRef:
    from service.tasks.reply_parser import parse_reply_reference
    return SourceRef("email", hints=parse_reply_reference(text).hints)


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
        reference_edit = _reference_correction(prompt) and prompt.lstrip()[:1] not in {'"', "'", "“", "‘"}
        if language_question(plan) and not reference_edit:
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
        elif _reference_turn(plan, prompt):
            update = _reference_update(plan, prompt)
            # Persist the entire transition even while another field is bad.
            # No error return may precede these updates or revision invalidation.
            plan.parameters["reference_hints"] = SlotValue(update.hints, "followup", original=prompt)
            plan.parameters["reply_pending_reference"] = SlotValue(prompt, "followup")
            plan.parameters["reply_reference_uncertain"] = SlotValue(update.uncertain, "followup")
            if update.schedule_requested:
                plan.parameters["schedule_requested"] = SlotValue(update.schedule_requested, "followup")
            if update.error:
                plan.parameters["reply_selector_error"] = SlotValue(update.error, "unresolved")
            else:
                plan.parameters.pop("reply_selector_error", None)
                plan.target = SlotValue(prompt.strip(), "followup", original=prompt)
            plan.resolved_references.pop("reply.target", None)
            plan.parameters.pop("source_candidates", None)
            plan.parameters.pop("source_coverage", None)
        else:
            return done("Choose one of the offered emails, or narrow its sender, topic or account.", "source_selection_needed")
        plan.revision += 1
        plan.parameters.pop("reply_args", None)

    plan.recompute_status()
    if error := plan.parameters.get("reply_selector_error"):
        return done(str(error.value) + " Nothing was sent.", "source_selector_ambiguous")
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
    stops_before_mail = _reply_stops_before_mail(compiled, active, prompt, now=now)
    should_warm = (needs_mail and not stops_before_mail
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
