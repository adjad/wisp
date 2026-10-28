"""Controlled execution for validated typed tasks."""
from __future__ import annotations

from datetime import datetime, timedelta

from service.safety.policy import Tier, decide
from service.tasks.models import OUTBOUND_INTENTS, TaskExecution, TaskPlan
from service.tools import access_needed
from service.tools.registry import classify_tool_outcome, get_tool, run_tool


def _verified_reminder_readback(assistant_store, args: dict) -> bool:
    if assistant_store is None:
        return False
    try:
        if args.get('all_day') is True:
            from service.assistant.verified_reminders import due_timestamp
            target = due_timestamp({'all_day': True, 'due_date': args['when_iso']})
        else:
            target = datetime.fromisoformat(str(args["when_iso"])).timestamp()
        wanted = " ".join(str(args["title"]).split()).casefold()
        # Verifying a create, not using a reminder: a hidden row still proves it landed.
        rows = assistant_store.active_between(target - 60, target + 60, include_stale=True)
    except (KeyError, TypeError, ValueError):
        return False
    # A verified native create is persisted as the source='reminders' row its
    # receipt inserts; the Wisp-only fallback stores source='manual'. EventKit
    # keeps minute precision, so compare due times by local minute.
    return any(
        row.get("source") in {"manual", "reminders"}
        and " ".join(str(row.get("title") or "").split()).casefold() == wanted
        and int(float(row.get("when_ts") or 0) // 60) == int(target // 60)
        and bool(row.get('all_day')) == bool(args.get('all_day'))
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
            if target.get('all_day') and len(plan.temporal.absolute_iso) == 10:
                from service.assistant.verified_reminders import due_timestamp
                when_ts = due_timestamp({'all_day': True, 'due_date': plan.temporal.absolute_iso})
            else:
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


def _verified_effect(plan: TaskPlan, assistant_store, *, executed_at: datetime,
                     step_args: dict | None = None) -> bool:
    if assistant_store is None:
        return False
    if plan.intent == "reminder.create":
        return _verified_reminder_readback(assistant_store, step_args if step_args is not None else plan.steps[0].args)
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
        target = plan.resolved_targets[0]
        expected_all_day = bool(target.get('all_day')) and (not plan.temporal.absolute_iso or len(plan.temporal.absolute_iso) == 10)
        wanted = " ".join(title.split()).casefold()
        return all(
            row is not None and row.get("status") == "active"
            and " ".join(str(row.get("title") or "").split()).casefold() == wanted
            and abs(float(row.get("when_ts") or 0) - when_ts) < 60
            and bool(row.get('all_day')) == expected_all_day
            for row in rows)
    return False


def _verified_send(plan: TaskPlan, raw: str) -> bool:
    """The send receipt must name the address we actually approved.

    `classify_tool_outcome` already requires the tool's success prefix, which
    only appears after the native bridge reports ok. This adds the second half
    of the §7 receipt contract: the resolved recipient.
    """
    if plan.intent == "email.reply":
        from service.tasks.reply_contract import verify_receipt
        return verify_receipt(raw, plan.steps[0].args.get("expected_reply", {}))
    address = str((plan.resolved_recipient or {}).get("address") or "").strip()
    return bool(address) and address.casefold() in str(raw or "").casefold()


def _result_text(plan: TaskPlan, status: str) -> str:
    if plan.intent in OUTBOUND_INTENTS:
        channel = "email" if plan.intent.startswith("email.") else "message"
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


def _batch_result(plan: TaskPlan, status: str, results: list[dict]) -> str:
    lines = []
    by_id = {item['id']: item for item in results}
    for step in plan.steps:
        call_id = f'task_{plan.id}_{step.id}_{plan.revision}'
        result = by_id.get(call_id)
        title = step.args['title']
        if result and result['status'] == 'succeeded':
            lines.append(str(result['result']))
        elif result and result['status'] == 'planned':
            label = 'all day' if step.args.get('all_day') else 'at the specified time'
            lines.append(f'Dry run only: {title} — {step.args["when_iso"]}, {label}.')
        elif result:
            lines.append(f'Not confirmed: {title}. {result["result"]}')
        elif call_id in plan.claimed_calls:
            lines.append(f'Already attempted or uncertain: {title}; check before retrying.')
        else:
            lines.append(f'Not attempted: {title}.')
    if status not in {'completed', 'planned'}:
        lines.append('I stopped the remaining work and will not replay these reminders automatically.')
    return '\n'.join(lines)


async def execute_task(plan: TaskPlan, emit, approver, *, test_mode: bool = False,
                       assistant_store=None, on_claim=None, now: datetime | None = None) -> TaskExecution:
    if plan.status != "running" or not plan.steps:
        return TaskExecution("failed", "The task was not in an executable state.", finalize=False)

    calls: list[dict] = []
    results: list[dict] = []
    from service.tasks.reminder_batch import is_batch, validated_items
    batch = is_batch(plan)
    def response(status):
        return _batch_result(plan, status, results) if batch else _result_text(plan, status)
    if batch:
        try:
            items = validated_items(plan, now=now or datetime.now())
            expected = [(item['id'], 'add_reminder',
                {'title': item['title'], 'when_iso': item['when_iso'], 'kind': 'reminder',
                 **({'all_day': True} if item['all_day'] else {})}) for item in items]
            if ([(step.id, step.tool, step.args) for step in plan.steps] != expected
                    or any(not step.effect or step.max_calls != 1 or step.depends_on
                           or step.success_statuses != ['succeeded'] for step in plan.steps)):
                raise ValueError('reminder steps differ from the complete bound request')
        except (KeyError, TypeError, ValueError, OverflowError):
            return TaskExecution('failed', 'The complete reminder request could not be validated. No tools ran.')
        if plan.claimed_calls:
            return TaskExecution('failed', response('duplicate'), finalize=False)
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
            return TaskExecution("failed", response("duplicate"),
                                 calls, results, finalize=False)

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
        needs: list[dict] = []
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
            if plan.intent == "email.reply":
                from service.tasks.reply_contract import preview
                action["preview"] = preview(step.args["expected_reply"])
            elif plan.intent in OUTBOUND_INTENTS:
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
            approved = await approver.confirm(action)
            # A concurrent executor may have claimed this very plan object
            # while our approval was open. Even denial must not close its send.
            if step.effect and call_id in plan.claimed_calls:
                return TaskExecution("failed", response("duplicate"),
                                     calls, results, finalize=False)
            if approved:
                if not claim():
                    return TaskExecution("failed", response("duplicate"),
                                         calls, results, finalize=False)
                from service.tools.action_tools import human_reviewed_content
                with human_reviewed_content(bool(action.get("preview"))), \
                        access_needed.noting() as needs:
                    raw = await run_tool(tool, step.args)
                outcome = classify_tool_outcome(step.tool, raw)
            else:
                raw = "The user denied this action."
                outcome = classify_tool_outcome(step.tool, raw, denied=True)
        else:
            if not claim():
                return TaskExecution("failed", response("duplicate"),
                                     calls, results, finalize=False)
            with access_needed.noting() as needs:
                raw = await run_tool(tool, step.args)
            outcome = classify_tool_outcome(step.tool, raw)
        if (not test_mode and outcome.status == "succeeded"
                and not (_verified_send(plan, raw)
                         if plan.intent in OUTBOUND_INTENTS
                         else _verified_effect(plan, assistant_store,
                                               executed_at=now or datetime.now(), step_args=step.args))):
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
        await emit({"type": "tool_result", **item, **access_needed.payload(needs)})

        if outcome.status == "planned":
            if batch:
                completed.add(step.id)
                continue
            return TaskExecution("planned", _result_text(plan, "planned"), calls, results)
        if outcome.status == "denied":
            return TaskExecution("denied", response("denied"), calls, results)
        if outcome.status not in step.success_statuses:
            return TaskExecution("failed", response("failed"), calls, results)
        completed.add(step.id)

    if batch:
        status = 'planned' if test_mode else 'completed'
        return TaskExecution(status, response(status), calls, results)
    receipt = results[-1]["result"] if results else ""
    if plan.intent == "email.reply":
        envelope = plan.steps[0].args["expected_reply"]
        receipt = f"Reply sent to {', '.join(envelope['to'])} from {envelope['from']}."
    return TaskExecution("completed", receipt, calls, results)
