"""Persistent workflow state machine used before ordinary routing."""
from __future__ import annotations

import time
import re

from service.reminder_intent import CAPABILITY_INVENTORY_RE
from service.workflows.compiler import (
    compile_decision, compile_new, extract_channel, extract_location,
    extract_recipient, extract_stock_symbols,
    is_assent, is_cancel, extract_sources,
)
from service.workflows.models import WorkflowPlan, WorkflowTurn


def _save(store, sid: str, plan: WorkflowPlan, event: str, payload: dict | None = None) -> None:
    store.save_workflow(sid, plan.to_dict())
    store.add_workflow_event(plan.id, event, payload)


def _question(plan: WorkflowPlan) -> str:
    if plan.status == "waiting_for_channel":
        return "Should I deliver that through Messages or email?"
    if plan.status == "waiting_for_recipient":
        return f"Who should I {('message' if plan.channel == 'messages' else 'email')} it to?"
    if plan.status == "waiting_for_time":
        return "When should I send it?"
    if plan.status == "waiting_for_location":
        return "Which city should I use for the weather?"
    if plan.status == "waiting_for_symbols":
        return "Which stock symbols or company names should I include?"
    return ""


def prepare_turn(store, sid: str, prompt: str, *, persist: bool = True) -> WorkflowTurn | None:
    """Compile a new task or advance the current task with this reply."""
    if CAPABILITY_INVENTORY_RE.search(prompt):
        return None
    new_plan = compile_new(
        prompt, last_user=(store.last_user_turn(sid) or "") if persist else "",
        last_assistant=(store.last_assistant_turn(sid) or "") if persist else "")
    active_raw = store.active_workflow(sid) if persist else None
    active = WorkflowPlan.from_dict(active_raw) if active_raw else None

    # A denied delivery is terminal. Short replies that only made sense as an
    # answer to it must not escape to the general agent and trigger a new tool.
    if active is None and persist:
        latest_raw = store.latest_workflow(sid)
        latest = WorkflowPlan.from_dict(latest_raw) if latest_raw else None
        plain_name = bool(
            re.fullmatch(
                r"[A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,2}",
                prompt.strip(),
            )
            and not re.search(r"\b(?:here|there|on|in|at|for|from)\b", prompt, re.I)
        )
        fragment = bool(
            is_assent(prompt)
            or re.fullmatch(r"[?!.]+", prompt.strip())
            or re.fullmatch(r"(?:messages?|texts?|i\s*message|e-?mail|mail)", prompt.strip(), re.I)
            or re.fullmatch(r"(?:generate|write|make)\s+it\s+(?:yourself|again)", prompt.strip(), re.I)
            or plain_name
            or bool(re.fullmatch(r"[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+", prompt.strip()))
        )
        if latest and latest.status == "cancelled" and fragment:
            return WorkflowTurn(
                latest,
                response=("The previous delivery was denied and is closed. Nothing was sent. "
                          "Start a new delivery request if you want to try again."),
                event="closed_delivery_followup",
            )

    # A referential correction is a revision of the source plan, not a new
    # message whose body happens to be our previous clarification question.
    correction = bool(re.search(r"\b(?:send|text|email|message)\s+(?:this|that|it)\b", prompt, re.I))
    if not active and persist and correction and extract_recipient(prompt):
        raw = store.latest_workflow(sid)
        if raw:
            active = WorkflowPlan.from_dict(raw)
            # Explicit readdressing gets a fresh approval, never revives a
            # denied send from a bare "yes" or a channel fragment.
            active.id = __import__("uuid").uuid4().hex
            active.status = "ready"
    if active and correction and not extract_sources(prompt):
        new_plan = None

    if new_plan is not None:
        if active is not None and active.id != new_plan.id:
            active.status = "superseded"
            if persist:
                _save(store, sid, active, "superseded", {"by": new_plan.id})
        plan = new_plan
        event = "created"
    elif active is not None:
        plan = active
        if is_cancel(prompt):
            plan.status = "cancelled"
            if persist:
                _save(store, sid, plan, "cancelled", {"reply": prompt})
            return WorkflowTurn(plan, response="Okay, I cancelled that request.", event="cancelled")

        if plan.status == "running" and is_assent(prompt):
            if time.time() - plan.updated_at < 300:
                return WorkflowTurn(
                    plan,
                    response="That request is already running. I won't start a duplicate.",
                    event="already_running",
                )
            # A process interruption can leave `running` on disk. After five
            # minutes an explicit retry is safe to compile as a fresh attempt;
            # immediate repeated confirmations are blocked above.
            plan.status = "failed"

        changed = False
        if correction:
            recipient = extract_recipient(prompt, plan.channel)
            channel = extract_channel(prompt)
            if recipient:
                plan.recipient = recipient
                changed = True
            if channel:
                plan.channel = channel
                changed = True
        if plan.status == "failed":
            if re.fullmatch(r"(?:it(?:'?s| is)\s+)?(?:on|in)\s+contacts[.!]?", prompt.strip(), re.I):
                changed = True
            # A failed contact lookup can be repaired without losing sources.
            recipient = extract_recipient(prompt, plan.channel)
            if recipient:
                plan.recipient = recipient
                changed = True
            channel = extract_channel(prompt)
            if channel:
                plan.channel = channel
                changed = True
        if plan.status == "waiting_for_channel":
            channel = extract_channel(prompt)
            if channel:
                plan.channel = channel
                changed = True
        if plan.status in {"waiting_for_recipient", "waiting_for_channel"}:
            recipient = extract_recipient(prompt, plan.channel)
            if (not recipient and plan.status == "waiting_for_recipient"
                    and re.fullmatch(r"[A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,2}", prompt.strip())
                    and not is_assent(prompt)
                    and not re.search(r"\b(?:contacts|email|messages|no|nope)\b", prompt, re.I)):
                recipient = prompt.strip()
            if recipient:
                plan.recipient = recipient
                changed = True
        if plan.status == "waiting_for_time":
            # Scheduled-send tool owns final natural-language time parsing; the
            # workflow only needs to know the user supplied a value.
            value = prompt.strip()
            if value and not is_assent(value):
                plan.when = value
                changed = True
        if plan.status == "waiting_for_location":
            location = extract_location(prompt, standalone=True)
            if location:
                plan.location = location
                plan.source_args["weather"] = {**plan.source_args.get("weather", {}), "location": location}
                changed = True
        if plan.status == "waiting_for_symbols":
            symbols = extract_stock_symbols(prompt, standalone=True)
            if symbols:
                plan.stock_symbols = symbols
                existing = plan.source_args.get("stock", {})
                plan.source_args["stock"] = {**existing, "symbols": symbols}
                changed = True
        assent_like = bool(re.fullmatch(
            r"\s*(?:yes|yep|yeah|ok(?:ay)?|sure|go ahead)"
            r"(?:\s+send\s+(?:this|that|it)(?:\s+only)?)?\s*[.!]?\s*",
            prompt, re.I,
        ))
        if not changed and not (plan.status in {"ready", "failed"} and is_assent(prompt)):
            if plan.status.startswith("waiting_for_") and (
                    is_assent(prompt) or assent_like or "contacts" in prompt.lower()
                    or re.fullmatch(r"[?!.]+", prompt.strip())):
                return WorkflowTurn(plan, response=_question(plan), event="clarification_repeated")
            return None
        if plan.last_error == "delivery outcome uncertain":
            return WorkflowTurn(plan, response="The earlier delivery may already have happened. "
                                "Please check the destination before starting a new send; I won't repeat it automatically.",
                                event="uncertain_delivery_blocked")
        plan.recompute_status()
        event = "updated" if changed else "retried"
    else:
        return None

    if plan.status.startswith("waiting_for_"):
        if persist:
            _save(store, sid, plan, event, {"status": plan.status})
        return WorkflowTurn(plan, response=_question(plan), event=event)

    plan.status = "running"
    plan.last_error = ""
    if persist:
        _save(store, sid, plan, "execution_started", {})
    return WorkflowTurn(plan, decision=compile_decision(plan), event="execution_started")


def finish_workflow(store, sid: str, plan: WorkflowPlan, captured: dict) -> str:
    """Set terminal/retry state from observed tool results, conservatively."""
    decision_effects = {"send_message", "send_email", "draft_message", "draft_email",
                        "schedule_send"}
    results = [item for item in captured.get("tool_results", [])
               if item.get("name") in decision_effects]
    calls = [item for item in captured.get("tool_calls", [])
             if item.get("name") in decision_effects]
    result_text = "\n".join(str(item.get("result") or item.get("content") or "")
                            for item in results).lower()
    denied = bool(captured.get("denied")) or "denied" in result_text or "not approved" in result_text
    from service.tools.registry import classify_tool_outcome
    expected = compile_decision(plan).tool_subset[-1]
    verified = [classify_tool_outcome(item.get("name", ""),
                str(item.get("result") or item.get("content") or ""))
                for item in results if item.get("name") == expected]

    if denied:
        plan.status = "cancelled"
        event = "approval_denied"
    elif (not any(item.get("name") == expected for item in calls)
          or not verified or verified[-1].status != "succeeded"):
        plan.status = "failed"
        plan.last_error = ("delivery outcome uncertain" if calls else
                           "delivery tool did not return a verified success")
        event = "execution_failed"
    else:
        plan.status = "completed"
        event = "completed"
    _save(store, sid, plan, event, {
        "effect_calls": [item.get("name") for item in calls],
        "effect_results": [str(item.get("result") or item.get("content") or "")[:500]
                           for item in results],
    })
    return plan.status
