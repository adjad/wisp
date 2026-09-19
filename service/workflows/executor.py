"""Execute a delivery plan without allowing a model to invent its payload.

Source text is quoted data, never instructions. All required reads must finish
before one immutable recipient/payload preview can be approved. No retries of
effects occur here, including after an uncertain receipt.
"""
from __future__ import annotations

import re
from datetime import datetime

from service.safety.policy import Tier, decide
from service.tasks.models import TaskExecution
from service.tools.registry import (DisplayOnlyToolResult, get_tool, run_tool,
                                    classify_tool_outcome, _validate_args)
from service.workflows.compiler import SOURCE_TO_TOOL, compile_decision
from service.workflows.present import compose


def usable_source(name: str, raw: str) -> bool:
    low = raw.strip().lower()
    return (bool(low) and classify_tool_outcome(name, raw).status == "succeeded"
            and not low.startswith("(")
            and not any(marker in low for marker in (
                "still syncing", "could not check", "may be incomplete",
                "incomplete stock lookup", "couldn't generate", "no inbox data",
                "could not be checked", "couldn't include", "not ready yet")))


def resolve_destination(recipient: str, channel: str) -> tuple[str, str]:
    from service.tools.action_tools import _resolve_recipient
    value = recipient.strip()
    email = re.fullmatch(r"[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+", value)
    phone = re.fullmatch(r"\+?[\d().\s-]{7,}", value)
    if email or (phone and channel == "messages"):
        return value, ""
    if phone and channel == "email":
        return "", "That is a phone number. What email address should I use?"
    if value.casefold() in {"me", "myself"}:
        return "", ("What email address should I use for you?" if channel == "email"
                    else "What phone number or Messages address should I use for you?")
    return _resolve_recipient(value, want_email=channel == "email")


async def execute_workflow(plan, emit, approver, *, test_mode=False, session_store=None) -> TaskExecution:
    calls, results = [], []
    news_used = bool(plan.news_artifact_provenance)

    def finish(status, response):
        if news_used:
            response = DisplayOnlyToolResult(response, model_text=(
                f"News delivery workflow ended with status '{status}'. "
                "The detailed receipt is displayed separately. Publisher text is not available in model history."), artifact_kind="receipt")
        return TaskExecution(status, response, calls, results)

    async def invoke(name, args, *, effect=False):
        nonlocal news_used
        tool = get_tool(name)
        if tool is None:
            return "failed", f"Required tool {name} is unavailable."
        problem = _validate_args(tool, args)
        if problem:
            return "failed", str(problem)
        policy = decide(tool.category, args, tool=name)
        cid = f"workflow_{plan.id}_{len(calls)}"
        call = {"id": cid, "name": name, "args": dict(args),
                "decision": policy.tier.value, "reason": policy.reason}
        calls.append(call)
        await emit({"type": "tool_call", **call})
        if test_mode:
            raw, status = "Dry run only — tool not executed.", "planned"
        elif policy.tier is Tier.DENY:
            raw, status = f"BLOCKED by safety policy: {policy.reason}", "denied"
        else:
            approved = True
            if effect or policy.tier is Tier.CONFIRM:
                from service.tools.action_tools import confirm_preview
                approved = await approver.confirm({
                    "id": cid, "tool": name, "args": dict(args),
                    "reason": policy.reason,
                    "preview": (f"Requested recipient: {plan.recipient}\n"
                                + (confirm_preview(name, args) or str(args)))})
            if approved:
                raw = await run_tool(tool, args)
                if isinstance(raw, DisplayOnlyToolResult):
                    news_used = True
                status = classify_tool_outcome(
                    name, raw.model_text if isinstance(raw, DisplayOnlyToolResult) else raw).status
            else:
                raw, status = "The user denied this action.", "denied"
        if effect and news_used and not isinstance(raw, DisplayOnlyToolResult):
            # A provider receipt may echo the outbound publisher text. Keep
            # persisted workflow evidence and retry state free of that text.
            safe_receipt = {
                "send_message": "Message sent to the approved recipient.",
                "send_email": "Email sent to the approved recipient.",
                "draft_message": "Message draft prepared in Wisp.",
                "draft_email": "Draft opened in Mail.",
                "schedule_send": "Scheduled: the approved news delivery.",
            }.get(name, "(error: unsupported delivery receipt.)") if status == "succeeded" else (
                "The user denied this action." if status == "denied"
                else "(error: delivery did not return a verified success.)")
            raw = DisplayOnlyToolResult(raw, model_text=safe_receipt, artifact_kind="receipt")
        item = {"id": cid, "name": name,
                "result": raw.model_text if isinstance(raw, DisplayOnlyToolResult) else raw,
                "status": status}
        results.append(item)
        await emit({"type": "tool_result", **item})
        return status, raw

    if plan.content_error:
        return finish("needs_input", plan.content_error)

    if plan.news_artifact_provenance:
        if session_store is None:
            from service.memory.store import store as session_store
        provenance = plan.news_artifact_provenance
        artifact = session_store.display_artifact(
            provenance.get("session_id", ""), provenance.get("turn_idx", -1))
        if (artifact is None or artifact.kind != "news"
                or artifact.provenance != provenance or artifact.text != plan.artifact_text):
            return finish("needs_input", "The referenced news display is unavailable or changed. "
                          "Please select the news again. Nothing was sent.")

    if plan.status != "running":
        return finish("failed", "The delivery plan is not ready.")
    if test_mode:
        # Do not read Contacts, inboxes or networks, or open real draft windows.
        for source in plan.sources:
            await invoke(SOURCE_TO_TOOL[source], plan.source_args.get(source, {}))
        return finish("planned", "Dry run only — no data was read and nothing was sent. "
                      "Delivery needs successful source reads and recipient validation.")

    destination, problem = resolve_destination(plan.recipient, plan.channel)
    if problem:
        return finish("failed", f"Nothing sent. {problem}")
    await emit({"type": "workflow", "event": "recipient_resolved",
                "workflow_id": plan.id, "requested_recipient": plan.recipient,
                "resolved_recipient": destination, "channel": plan.channel})

    sections = []
    if "weather" in plan.sources and re.search(r"\b(?:week|month|year)\b", plan.date_range):
        return finish("failed", "Nothing sent: the weather tool covers only three days, "
                      "not the requested range. Would a three-day forecast be useful?")
    for source in plan.sources:
        name = SOURCE_TO_TOOL[source]
        status, raw = await invoke(name, plan.source_args.get(source, {}))
        if status != "succeeded" or not usable_source(name, raw):
            return finish("failed", f"Nothing sent: the required {source} lookup "
                          f"did not produce a complete verified result.\n{raw}")
        # No model rewrite: `present` is deterministic code, so quotations keep
        # source ownership, dates, currency/percent units, and the warning about
        # an unverified Apple mirror. What it does drop is the scaffolding those
        # strings carry for the model — see service/workflows/present.py.
        sections.append((source, raw))
    from service.tools.action_tools import normalize_outbound_text
    body = normalize_outbound_text(plan.artifact_text or compose(sections))
    if not (plan.artifact_text or sections):
        return finish("failed", "Nothing sent: no source content was available.")
    if len(body) > 18000:
        return finish("failed", "Nothing sent: the report is too long for a complete reviewable preview. Please narrow the date range or sources.")
    effect = compile_decision(plan).tool_subset[-1]
    args = {"to": destination}
    args["text" if effect in {"send_message", "draft_message"} else "body"] = body
    if plan.channel == "email":
        args["subject"] = "Wisp report" + (f" — {plan.date_range}" if plan.date_range else "")
    if effect == "schedule_send":
        from service.tools.timeranges import resolve_when, BadWhen
        try:
            when, _ = resolve_when(plan.when)
        except BadWhen as exc:
            return finish("failed", f"Nothing scheduled. {exc}")
        if when.timestamp() <= datetime.now().timestamp():
            return finish("failed", "Nothing scheduled: that time has passed. What future time should I use?")
        args.update(channel="message" if plan.channel == "messages" else "email",
                    when=when.isoformat())
    status, raw = await invoke(effect, args, effect=True)
    if status == "succeeded" and effect == "draft_message":
        await emit({"type": "message_draft", "to": destination, "text": body})
    return finish("completed" if status == "succeeded" else status, raw)
