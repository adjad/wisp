"""Persistent workflow state machine used before ordinary routing."""
from __future__ import annotations

import time
import re

from service.reminder_intent import CAPABILITY_INVENTORY_RE
from service.workflows.compiler import (
    compile_decision, compile_new, extract_channel, extract_location,
    extract_recipient, extract_stock_symbols,
    is_assent, is_cancel, extract_sources,
    plain_reference_request, explicit_delivery, extract_delivery_time, CONTENT_QUESTION,
)
from service.workflows.models import WorkflowPlan, WorkflowTurn


def _save(store, sid: str, plan: WorkflowPlan, event: str,
          payload: dict | None = None) -> bool:
    expected = plan.revision
    plan.revision += 1
    plan.updated_at = time.time()
    if not store.save_workflow_revision(
            sid, plan.to_dict(), expected_revision=expected):
        plan.revision = expected
        return False
    store.add_workflow_event(plan.id, event, payload)
    return True


def _stale_turn(store, sid: str, plan: WorkflowPlan) -> WorkflowTurn:
    current = store.workflow_state(sid, plan.id)
    if current:
        plan = WorkflowPlan.from_dict(current)
    return WorkflowTurn(
        plan,
        response=("That delivery request changed before this step could finish. "
                  "I did not continue the older version."),
        event="stale_workflow",
    )


def _question(plan: WorkflowPlan) -> str:
    if plan.status == "waiting_for_content":
        return plan.content_error
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


def prepare_news_selector_guard(store, sid: str, prompt: str) -> WorkflowTurn | None:
    """Claim only stored-news references before typed-task routing.

    The predicate is intentionally compiler-backed: authored prose that merely
    mentions a story must not become a request to disclose stored publisher text.
    Existing typed tasks and unrelated workflows retain precedence.
    """
    active_task = getattr(store, "active_task", None)
    if active_task is not None and active_task(sid):
        return None
    active_raw = store.active_workflow(sid)
    if active_raw:
        try:
            active = WorkflowPlan.from_dict(active_raw)
        except ValueError:
            if ("news_clarification_provenance" in active_raw
                    or "news_artifact_provenance" in active_raw
                    or active_raw.get("status") == "waiting_for_content"):
                plan = WorkflowPlan(content_error=CONTENT_QUESTION)
                plan.recompute_status()
                return WorkflowTurn(plan, response=_question(plan), event="invalid_news_provenance")
            return None
        if active.news_clarification_provenance or active.news_artifact_provenance:
            return prepare_turn(store, sid, prompt)
        return None
    artifact = store.display_artifact(sid)
    if artifact is None or artifact.kind != "news":
        return None
    candidate = compile_new(
        prompt,
        last_user=store.last_user_turn(sid) or "",
        last_assistant=store.last_assistant_turn(sid) or "",
        prior_display=artifact,
    )
    if candidate is None or not (
            candidate.news_artifact_provenance or candidate.news_clarification_provenance):
        return None
    return prepare_turn(store, sid, prompt)


def prepare_turn(store, sid: str, prompt: str, *, persist: bool = True) -> WorkflowTurn | None:
    """Compile a new task or advance the current task with this reply."""
    if CAPABILITY_INVENTORY_RE.search(prompt):
        return None
    prior_display = store.display_artifact(sid) if persist else None
    if (prior_display is not None and prior_display.kind != "news"
            and re.search(r"\b(?:send|text|email|share|forward)\s+(?:this|that|it)\b", prompt, re.I)):
        return WorkflowTurn(WorkflowPlan(status="cancelled"), response=(
            "That answer contains several kinds of content. Which news story or source "
            "should I deliver? Nothing was sent."), event="clarify_display_source")
    # Check durable attempts before correction/recompilation can allocate a
    # new id. An interrupted external call has no reliable success receipt;
    # age and changed arguments cannot make it safe to repeat. A cancellation
    # closes this uncertain workflow, without claiming the external action
    # was cancelled or never happened.
    if persist:
        latest = store.latest_workflow(sid, max_age_seconds=float("inf"))
        if (latest and latest.get("status") in {"running", "ready", "failed"}
                and store.workflow_effect_claimed(latest["id"])):
            plan = WorkflowPlan.from_dict(latest)
            if is_cancel(prompt):
                plan.status = "cancelled"
                if not _save(store, sid, plan, "uncertain_delivery_closed"):
                    return _stale_turn(store, sid, plan)
            return WorkflowTurn(
                plan, response=("The earlier delivery may already have happened. "
                                "Check the destination before starting a new request. "
                                + ("I closed this pending workflow." if is_cancel(prompt) else
                                   "Cancel this pending workflow before making a new delivery request; "
                                   "I will not repeat it automatically.")),
                event="uncertain_delivery_blocked")
    new_plan = compile_new(
        prompt, last_user=(store.last_user_turn(sid) or "") if persist else "",
        last_assistant=(store.last_assistant_turn(sid) or "") if persist else "",
        prior_display=prior_display)
    active_raw = store.active_workflow(sid) if persist else None
    # Older session stores enumerate known active statuses in SQL. Recover
    # this new clarification state without migrating or widening that query.
    if not active_raw and persist:
        latest = store.latest_workflow(sid, max_age_seconds=21600)
        if (latest and latest.get("status") == "waiting_for_content"
                and time.time() - latest.get("updated_at", 0) <= 21600):
            active_raw = latest
    active = WorkflowPlan.from_dict(active_raw) if active_raw else None
    # Content corrections during channel/recipient clarification replace the
    # payload scope; they must not leave the old artifact available to retry.
    scope_correction = bool(re.search(
            r"\b(?:only|just|instead|shorten|shorter|rewrite|rephrase|translate|"
            r"translation|condense|exclude|except|without|part|section|bullet|sentence|paragraph)\b", prompt, re.I))
    if re.fullmatch(r"\s*(?:yes|yep|yeah|ok(?:ay)?|sure)\s+send\s+"
                    r"(?:this|that|it)\s+only\s*[.!]?\s*", prompt, re.I):
        scope_correction = False
    simple_channel = bool(re.fullmatch(
        r"\s*(?:(?:use|via|through)\s+)?(?:messages?|texts?|texxt|imessage|e-?mail)"
        r"(?:\s+message)?\s*[.!]?\s*",
        prompt, re.I))
    simple_reply = (simple_channel or is_assent(prompt) or is_cancel(prompt)
                    or plain_reference_request(prompt)
                    or bool(re.fullmatch(r"[?!.]+", prompt.strip()))
                    or bool(re.fullmatch(r"\+?[\d().\s-]{7,}", prompt.strip())))
    if active and active.status == "waiting_for_recipient":
        # A bare single contact label is a slot answer. Free-form sentences
        # require an explicit new request; otherwise edit instructions can be
        # mistaken for a two- or three-word contact name.
        simple_reply = simple_reply or bool(re.fullmatch(r"[A-Za-z][A-Za-z'-]*", prompt.strip()))
    # Unknown prose in a channel answer can contain an unrecognized content
    # edit. Never consume just the channel and silently discard the rest.
    if (active and active.status in {"waiting_for_channel", "waiting_for_recipient", "ready", "failed"}
            and new_plan is None and not simple_reply and not scope_correction
            and not re.fullmatch(r"[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+", prompt.strip())
            and not re.fullmatch(r"(?:it(?:'?s| is)\s+)?(?:on|in)\s+contacts[.!]?", prompt.strip(), re.I)):
        new_plan = WorkflowPlan(original_request=prompt, content_error=CONTENT_QUESTION)
        new_plan.recompute_status()
    if active and scope_correction:
        if new_plan is None:
            new_plan = compile_new(f"send {prompt}")
    if active and new_plan and (scope_correction or not explicit_delivery(prompt)):
        # Parse destination from the original fragment. Prepending 'send'
        # must not turn 'email section' into a contact named 'section'.
        explicit_recipient = extract_recipient(prompt)
        if not explicit_recipient and re.search(r"\bto\b|@", prompt, re.I):
            explicit_recipient = new_plan.recipient
        new_plan.recipient = explicit_recipient or active.recipient
        new_plan.channel = new_plan.channel or active.channel
        mode = explicit_delivery(prompt)
        new_plan.delivery = mode or active.delivery
        new_plan.when = (new_plan.when if mode == "scheduled" else
                         ("" if mode else active.when))
        new_plan.recompute_status()

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
    correction = bool(re.search(r"\b(?:send|text|email|message|draft|compose|write|share|forward)"
                                r"\s+(?:this|that|it)\b", prompt, re.I))
    if not active and persist and correction and extract_recipient(prompt):
        raw = store.latest_workflow(sid)
        if raw:
            if store.workflow_effect_claimed(raw["id"]) and not extract_sources(prompt):
                return WorkflowTurn(
                    WorkflowPlan.from_dict(raw),
                    response="That delivery was already attempted. Please make a new request "
                             "with an explicit source after checking the destination.",
                    event="uncertain_delivery_blocked")
            active = WorkflowPlan.from_dict(raw)
            # Explicit readdressing gets a fresh approval, never revives a
            # denied send from a bare "yes" or a channel fragment.
            active.id = __import__("uuid").uuid4().hex
            active.revision = 0
            active.status = "ready"
    if (active and correction and not extract_sources(prompt)
            and plain_reference_request(prompt)):
        new_plan = None

    if new_plan is not None:
        if active is not None and active.id != new_plan.id:
            active.status = "superseded"
            active.news_clarification_provenance = {}
            if persist:
                if not _save(store, sid, active, "superseded", {"by": new_plan.id}):
                    return _stale_turn(store, sid, active)
        plan = new_plan
        event = "created"
    elif active is not None:
        plan = active
        if is_cancel(prompt):
            plan.status = "cancelled"
            plan.news_clarification_provenance = {}
            if persist:
                if not _save(store, sid, plan, "cancelled", {"reply": prompt}):
                    return _stale_turn(store, sid, plan)
            return WorkflowTurn(plan, response="Okay, I cancelled that request.", event="cancelled")

        if plan.content_error:
            return WorkflowTurn(plan, response=_question(plan), event="clarification_repeated")

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
            if mode := explicit_delivery(prompt):
                when = extract_delivery_time(prompt) if mode == "scheduled" else ""
                changed = changed or mode != plan.delivery or when != plan.when
                plan.delivery = mode
                plan.when = when
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
            if not _save(store, sid, plan, event, {"status": plan.status}):
                return _stale_turn(store, sid, plan)
        return WorkflowTurn(plan, response=_question(plan), event=event)

    plan.status = "running"
    plan.last_error = ""
    if persist:
        if not _save(store, sid, plan, "execution_started", {}):
            return _stale_turn(store, sid, plan)
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

    terminal = WorkflowPlan.from_dict(plan.to_dict())
    if denied:
        terminal.status = "cancelled"
        event = "approval_denied"
    elif (not any(item.get("name") == expected for item in calls)
          or not verified or verified[-1].status != "succeeded"):
        terminal.status = "failed"
        terminal.last_error = ("delivery outcome uncertain" if calls else
                               "delivery tool did not return a verified success")
        event = "execution_failed"
    else:
        terminal.status = "completed"
        event = "completed"
    terminal.revision = plan.revision + 1
    terminal.updated_at = time.time()
    transitioned = store.transition_workflow(
        sid, terminal.to_dict(), from_status="running",
        expected_revision=plan.revision)
    if not transitioned:
        current = store.workflow_state(sid, plan.id)
        if current:
            persisted = WorkflowPlan.from_dict(current)
            plan.status = persisted.status
            plan.revision = persisted.revision
            plan.last_error = persisted.last_error
        else:
            # Direct provenance-bound harnesses validate an artifact from a
            # session store without first persisting a workflow row. They still
            # need the observed terminal outcome; no durable transition exists
            # to update in that case.
            plan.status = terminal.status
            plan.revision = terminal.revision
            plan.last_error = terminal.last_error
        return plan.status
    plan.status = terminal.status
    plan.revision = terminal.revision
    plan.last_error = terminal.last_error
    store.add_workflow_event(plan.id, event, {
        "effect_calls": [item.get("name") for item in calls],
        "effect_results": [str(item.get("result") or item.get("content") or "")[:500]
                           for item in results],
    })
    return plan.status
