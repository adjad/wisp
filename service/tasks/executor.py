"""Controlled execution for validated typed tasks."""
from __future__ import annotations

from datetime import datetime, timedelta

from service.safety.policy import Tier, decide
from service.tasks.models import OUTBOUND_INTENTS, TaskExecution, TaskPlan
from service.tools.registry import classify_tool_outcome, get_tool, run_tool


def _verified_reminder_readback(assistant_store, args: dict) -> bool:
    if assistant_store is None:
        return False
    try:
        target = datetime.fromisoformat(str(args["when_iso"])).timestamp()
        wanted = " ".join(str(args["title"]).split()).casefold()
        rows = assistant_store.active_between(target - 60, target + 60)
    except (KeyError, TypeError, ValueError):
        return False
    return any(
        row.get("source") == "manual"
        and " ".join(str(row.get("title") or "").split()).casefold() == wanted
        and abs(float(row.get("when_ts") or 0) - target) < 1
        for row in rows)


def _target_ids(plan: TaskPlan) -> list[str]:
    ids: list[str] = []
    for target in plan.resolved_targets:
        ids.append(str(target.get("id") or ""))
        ids.extend(str(value) for value in target.get("duplicate_ids") or [])
    return [value for value in dict.fromkeys(ids) if value]


def _expected_update(plan: TaskPlan, *, executed_at: datetime) -> tuple[str, float] | None:
    if len(plan.resolved_targets) != 1:
        return None
    target = plan.resolved_targets[0]
    title = str(target.get("title") or "")
    new_title = plan.parameters.get("new_title")
    if new_title and str(new_title.value or "").strip():
        title = str(new_title.value).strip()
    if plan.temporal.absolute_iso:
        try:
            when_ts = datetime.fromisoformat(plan.temporal.absolute_iso).timestamp()
        except ValueError:
            return None
    else:
        day = str(plan.parameters.get("day").value or "") if plan.parameters.get("day") else ""
        if day not in {"today", "tomorrow"}:
            return None
        old = datetime.fromtimestamp(float(target.get("when_ts") or 0))
        base = executed_at.replace(hour=0, minute=0, second=0, microsecond=0)
        date = base + timedelta(days=1 if day == "tomorrow" else 0)
        when_ts = date.replace(hour=old.hour, minute=old.minute).timestamp()
    return title, when_ts


def _verified_effect(plan: TaskPlan, assistant_store, *, executed_at: datetime) -> bool:
    if assistant_store is None:
        return False
    if plan.intent == "reminder.create":
        return _verified_reminder_readback(assistant_store, plan.steps[0].args)
    ids = _target_ids(plan)
    if not ids:
        return False
    rows = [assistant_store.get(cid) for cid in ids]
    if plan.intent == "reminder.complete":
        return all(row is not None and row.get("status") == "done" for row in rows)
    if plan.intent == "reminder.delete":
        return all(row is None or row.get("status") != "active" for row in rows)
    if plan.intent == "reminder.update":
        expected = _expected_update(plan, executed_at=executed_at)
        if expected is None:
            return False
        title, when_ts = expected
        wanted = " ".join(title.split()).casefold()
        return all(
            row is not None and row.get("status") == "active"
            and " ".join(str(row.get("title") or "").split()).casefold() == wanted
            and abs(float(row.get("when_ts") or 0) - when_ts) < 60
            for row in rows)
    return False


def _verified_send(plan: TaskPlan, raw: str) -> bool:
    """The send receipt must name the address we actually approved.

    `classify_tool_outcome` already requires the tool's success prefix, which
    only appears after the native bridge reports ok. This adds the second half
    of the §7 receipt contract: the resolved recipient.
    """
    address = str((plan.resolved_recipient or {}).get("address") or "").strip()
    return bool(address) and address.casefold() in str(raw or "").casefold()


def _result_text(plan: TaskPlan, status: str) -> str:
    if plan.intent in OUTBOUND_INTENTS:
        channel = "email" if plan.intent == "email.send" else "message"
        verb = "scheduled" if plan.temporal.absolute_iso else "sent"
        return {
            "planned": f"Dry run only — the {channel} would be {verb} to "
                       f"{(plan.resolved_recipient or {}).get('address', '')} "
                       "exactly as shown above.",
            "denied": f"Okay — I didn’t {'schedule' if verb == 'scheduled' else 'send'} "
                      f"the {channel}.",
            "failed": f"The {channel} was not confirmed as {verb}. I won’t retry "
                      "automatically; check before sending again.",
            "duplicate": f"That {channel} was already attempted on this "
                         "revision, so I won’t send it again. Ask me to send "
                         "it again explicitly if it really didn’t arrive.",
        }[status]
    if plan.intent == "reminder.create":
        return {
            "planned": "Dry run only — the reminder would be created with the values shown above.",
            "denied": "Okay — I didn’t create the reminder.",
            "failed": "Reminder creation was not verified. I won't retry automatically; check the reminder list before creating another.",
            "duplicate": "That reminder was already attempted on this revision, so I won't create it again.",
        }[status]
    noun = {
        "reminder.update": "update the reminder",
        "reminder.complete": "mark the reminder done",
        "reminder.delete": "delete the reminder(s)",
    }.get(plan.intent, "complete the reminder task")
    if status == "duplicate":
        return f"Wisp already attempted to {noun} on this revision; it won't repeat it."
    if status == "planned":
        return f"Dry run only — Wisp would {noun} with the values shown above."
    if status == "denied":
        return f"Okay — I didn’t {noun}."
    return f"I couldn’t {noun}. The requested change was not verified."


async def execute_task(plan: TaskPlan, emit, approver, *, test_mode: bool = False,
                       assistant_store=None, on_claim=None) -> TaskExecution:
    if plan.status != "running" or not plan.steps:
        return TaskExecution("failed", "The task was not in an executable state.")

    calls: list[dict] = []
    results: list[dict] = []
    completed: set[str] = set()
    for step in plan.steps:
        if any(dep not in completed for dep in step.depends_on):
            return TaskExecution("failed", "A required task step did not complete.", calls, results)
        tool = get_tool(step.tool)
        if tool is None:
            return TaskExecution("failed", f"Required tool {step.tool} is unavailable.", calls, results)
        call_id = f"task_{plan.id}_{step.id}_{plan.revision}"
        if step.effect and call_id in plan.claimed_calls:
            # max_calls=1 and the idempotency key only mean something if the
            # boundary that runs the effect enforces them.
            return TaskExecution("failed", _result_text(plan, "duplicate"),
                                 calls, results)

        def claim() -> bool:
            """Take the right to run this effect. False = someone else has it.

            Called immediately before the tool runs, NOT before awaiting
            approval: two executions can sit in `confirm` at the same time, so
            a check made earlier proves nothing by the time the send goes out.
            """
            if not step.effect:
                return True
            if call_id in plan.claimed_calls:
                return False
            # Recorded on the plan first, so whatever `on_claim` persists
            # already carries the claim. Rolled back only if we lost the race.
            plan.claimed_calls.append(call_id)
            if on_claim is not None and not on_claim(plan, call_id):
                plan.claimed_calls.remove(call_id)
                return False
            return True

        policy = decide(tool.category, step.args, tool=step.tool)
        call = {"id": call_id, "name": step.tool, "args": dict(step.args),
                "decision": policy.tier.value, "reason": policy.reason,
                "task_id": plan.id, "task_revision": plan.revision}
        calls.append(call)
        await emit({"type": "tool_call", **call,
                    **({"test_mode": True} if test_mode else {})})
        if test_mode:
            raw = "Dry run only — the tool was planned but not executed."
            outcome = classify_tool_outcome(step.tool, raw, planned=True)
        elif policy.tier is Tier.DENY:
            raw = f"BLOCKED by safety policy: {policy.reason}"
            outcome = classify_tool_outcome(step.tool, raw, denied=True)
        elif policy.tier is Tier.CONFIRM:
            action = {"id": call_id, "tool": step.tool, "args": step.args,
                      "reason": policy.reason, "task_id": plan.id,
                      "task_revision": plan.revision}
            if plan.intent in OUTBOUND_INTENTS:
                # The remediation contract: approval shows the name the user
                # asked for AND the destination it resolved to, so a wrong
                # contact is visible before the send, not after.
                resolved = plan.resolved_recipient or {}
                requested = str((plan.recipient.value if plan.recipient else "")
                                or "").strip()
                address = str(resolved.get("address") or "")
                destination = (f"{requested} → {address}"
                               if requested and requested.casefold() != address.casefold()
                               else address)
                lines = [f"To: {destination}"]
                if plan.temporal.absolute_iso:
                    when = datetime.fromisoformat(plan.temporal.absolute_iso)
                    lines.append(f"When: {when:%a %b %-d at %-I:%M %p}")
                if plan.intent == "email.send":
                    lines.append(f"Subject: {step.args.get('subject', '')}")
                lines.append("")
                lines.append(str(step.args.get("text") or step.args.get("body") or ""))
                action["preview"] = "\n".join(lines)
            if step.tool == "clear_reminders":
                rows = [
                    f"{datetime.fromtimestamp(float(item['when_ts'])):%a %b %-d, %Y}  {item['title']}"
                    for item in plan.resolved_targets]
                action["reason"] = (
                    f"permanently deletes {len(rows)} reminder(s) — always confirmed")
                action["preview"] = "\n".join(rows[:60]) + (
                    f"\n…and {len(rows) - 60} more" if len(rows) > 60 else "")
            if await approver.confirm(action):
                if not claim():
                    return TaskExecution("failed", _result_text(plan, "duplicate"),
                                         calls, results)
                raw = await run_tool(tool, step.args)
                outcome = classify_tool_outcome(step.tool, raw)
            else:
                raw = "The user denied this action."
                outcome = classify_tool_outcome(step.tool, raw, denied=True)
        else:
            if not claim():
                return TaskExecution("failed", _result_text(plan, "duplicate"),
                                     calls, results)
            raw = await run_tool(tool, step.args)
            outcome = classify_tool_outcome(step.tool, raw)
        if (not test_mode and outcome.status == "succeeded"
                and not (_verified_send(plan, raw)
                         if plan.intent in OUTBOUND_INTENTS
                         else _verified_effect(plan, assistant_store,
                                               executed_at=datetime.now()))):
            raw = ("The tool returned without the expected receipt. "
                   "No verified success receipt was found.")
            outcome = classify_tool_outcome(step.tool, raw)
            # add_reminder's classifier accepts only its success prefix, but
            # make the verifier's rejection explicit if that ever changes.
            if outcome.status == "succeeded":
                outcome = type(outcome)("failed", raw, outcome.effect, outcome.facts)
        item = {"id": call_id, "name": step.tool, "result": raw,
                "status": outcome.status}
        results.append(item)
        await emit({"type": "tool_result", **item})

        if outcome.status == "planned":
            return TaskExecution("planned", _result_text(plan, "planned"), calls, results)
        if outcome.status == "denied":
            return TaskExecution("denied", _result_text(plan, "denied"), calls, results)
        if outcome.status not in step.success_statuses:
            return TaskExecution("failed", _result_text(plan, "failed"), calls, results)
        completed.add(step.id)

    receipt = results[-1]["result"] if results else ""
    return TaskExecution("completed", receipt, calls, results)
