"""Compile validated typed tasks into exact tool steps."""
from __future__ import annotations

from service.tasks.models import CHANNEL_FOR_INTENT, OUTBOUND_INTENTS, StepPlan, TaskPlan


class InvalidTaskPlan(ValueError):
    pass


def plan_task(plan: TaskPlan) -> list[StepPlan]:
    plan.recompute_status()
    if plan.status != "ready":
        raise InvalidTaskPlan(f"task is {plan.status}: {plan.missing_slots}")
    if plan.intent == "email.reply":
        from service.tasks.reply_contract import validate_envelope
        args = plan.parameters["reply_args"].value
        source = plan.resolved_references["reply.target"]["fields"]
        envelope = validate_envelope(args.get("expected_reply"))
        if (envelope is None or args.get("message_id") != source.get("message_id")
                or args.get("account") != source.get("account")
                or envelope["message_id"] != args["message_id"]
                or envelope["account"] != args["account"]
                or args.get("body") != plan.subject.value
                or bool(args.get("reply_all")) != bool(plan.parameters["reply_all"].value)
                or plan.channel.value != "email"):
            raise InvalidTaskPlan("reply envelope does not match the selected source and body")
        plan.steps = [StepPlan(id="reply_email", tool="reply_to_email", args=dict(args), effect=True)]
        return plan.steps
    if plan.intent in OUTBOUND_INTENTS:
        if plan.channel.value != CHANNEL_FOR_INTENT[plan.intent]:
            raise InvalidTaskPlan(
                f"{plan.intent} must target {CHANNEL_FOR_INTENT[plan.intent]}")
        # The address is an invariant, not something a step may go and find:
        # approval binds to these exact args.
        resolved = plan.resolved_recipient or {}
        address = str(resolved.get("address") or "").strip()
        if not address:
            raise InvalidTaskPlan(f"{plan.intent} needs a resolved recipient")
        if resolved.get("channel") != plan.channel.value:
            raise InvalidTaskPlan(
                "the resolved recipient belongs to a different channel")
        if plan.intent == "email.send" and "@" not in address:
            raise InvalidTaskPlan("email.send needs an email address")
        body = str(plan.subject.value or "").strip()
        if not body:
            raise InvalidTaskPlan(f"{plan.intent} needs a body")
        subject = (str(plan.parameters["email_subject"].value or "").strip()
                   if plan.intent == "email.send" else "")
        if plan.temporal.absolute_iso:
            # A scheduled send binds the address HERE, at approval time, and
            # never re-resolves when it fires: the user approved this exact
            # destination, and re-resolving later could deliver to a different
            # person without any approval at all.
            plan.steps = [StepPlan(
                id="schedule_send", tool="schedule_send",
                args={"channel": "email" if plan.intent == "email.send" else "message",
                      "to": address, "when": plan.temporal.absolute_iso,
                      "body": body,
                      **({"subject": subject} if subject else {})},
                max_calls=1, effect=True)]
        elif plan.intent == "message.send":
            plan.steps = [StepPlan(
                id="send_message", tool="send_message",
                args={"to": address, "text": body}, max_calls=1, effect=True)]
        else:
            plan.steps = [StepPlan(
                id="send_email", tool="send_email",
                args={"to": address, "subject": subject, "body": body},
                max_calls=1, effect=True)]
        return plan.steps
    if plan.recipient is not None:
        raise InvalidTaskPlan("reminder tasks cannot have an outbound recipient")
    if plan.channel.value != "reminders":
        raise InvalidTaskPlan("reminder tasks must target Reminders")

    if plan.intent == "reminder.create":
        title = str(plan.subject.value or "").strip()
        when_iso = plan.temporal.absolute_iso
        if not title or not when_iso:
            raise InvalidTaskPlan("reminder.create needs a subject and canonical time")
        plan.steps = [StepPlan(
            id="create_reminder", tool="add_reminder",
            args={"title": title, "when_iso": when_iso, "kind": "reminder"},
            max_calls=1, effect=True)]
    elif plan.intent == "reminder.update":
        if len(plan.resolved_targets) != 1:
            raise InvalidTaskPlan("reminder.update needs exactly one resolved target")
        current = plan.resolved_targets[0]
        args = {"title": str(current.get("title") or ""),
                "expected_id": str(current.get("id") or "")}
        if plan.temporal.absolute_iso:
            args["when_iso"] = plan.temporal.absolute_iso
        if day := str(plan.parameters.get("day").value or "") if plan.parameters.get("day") else "":
            args["day"] = day
        if new_title := str(plan.parameters.get("new_title").value or "") if plan.parameters.get("new_title") else "":
            args["new_title"] = new_title
        plan.steps = [StepPlan(
            id="update_reminder", tool="update_reminder", args=args,
            max_calls=1, effect=True)]
    elif plan.intent == "reminder.complete":
        if len(plan.resolved_targets) != 1:
            raise InvalidTaskPlan("reminder.complete needs exactly one resolved target")
        plan.steps = [StepPlan(
            id="complete_reminder", tool="complete_reminder",
            args={"title": str(plan.resolved_targets[0].get("title") or ""),
                  "expected_id": str(plan.resolved_targets[0].get("id") or "")},
            max_calls=1, effect=True)]
    elif plan.intent == "reminder.delete":
        scope = str(plan.parameters["scope"].value or "")
        query = str(plan.target.value or "").strip()
        if not plan.resolved_targets:
            raise InvalidTaskPlan("reminder.delete needs a nonempty resolved target set")
        plan.steps = [StepPlan(
            id="delete_reminders", tool="clear_reminders",
            args={"scope": scope,
                  **({"query": query} if query else {}),
                  "expected_ids": [str(item.get("id") or "")
                                   for item in plan.resolved_targets]},
            max_calls=1, effect=True)]
    else:
        raise InvalidTaskPlan(f"unsupported typed intent: {plan.intent}")
    return plan.steps
