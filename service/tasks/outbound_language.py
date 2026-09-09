"""Clarify ambiguous outbound wording before any recipient or source effects.

This is a bounded grammar, not a general natural-language classifier. Quoted
text and an explicit exact-words answer are literal contracts. Unquoted source
instructions must not become a payload merely because they lack a source noun.
"""
from __future__ import annotations

import re

from service.tasks.models import SlotValue, TaskPlan


def quoted(text: str) -> bool:
    text = text.strip()
    return len(text) > 1 and (text[0], text[-1]) in {
        ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}


_CONTENT_INSTRUCTION = re.compile(
    r"^(?:what(?:ever)?|which(?:ever)?|whether|how\b(?!\s+are\s+you)|"
    r"(?:please\s+)?(?:summarize|summarise|look\s+up|retrieve|compose|draft|write)\b|"
    r"(?:a|the|my)\s+(?:summary|briefing|recap)\b|"
    r"(?:(?:my|the|his|her|our|their|your|[\w’']+[’']s)\s+)?"
    r"(?:latest\s+|last\s+)?(?:calendar|schedule|agenda|emails?|mail|weather|forecast|"
    r"stocks?|news|headlines?|reminders?|notes?|score|results?)\b"
    r"(?!\s+(?:is|isn't|isn’t|was|wasn't|wasn’t|are|were|came|arrived|looks|looked|has|have)\b))",
    re.I)
_TRAILING_WHEN = re.compile(
    r"\s+(?P<when>at\s+(?:\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|noon|midnight)|"
    r"in\s+(?:\d+|one|two|three|four|five|ten|fifteen|twenty|thirty|an?)\s+(?:minutes?|hours?|days?)|"
    r"(?:(?:on|this|next)\s+)?(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+(?:morning|afternoon|evening))?"
    r"(?:\s+at\s+(?:\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|noon|midnight))?|"
    r"(?:this\s+)?(?:morning|afternoon|evening)|tonight)\s*[.!]?\s*$", re.I)


def mark_body_ambiguity(plan: TaskPlan, raw_body: str, *, scheduled: bool = False) -> None:
    if not raw_body.strip() or quoted(raw_body):
        return
    if _CONTENT_INSTRUCTION.search(raw_body.strip()):
        plan.parameters["content_clarification"] = SlotValue(raw_body.strip(), "unresolved")
    # Never silently take a trailing time out of the body or assume it belongs
    # to the body. Explicit quoting or a pre-body delivery time removes this
    # ambiguity. Preserve both possible payloads until the user chooses.
    if not scheduled and (match := _TRAILING_WHEN.search(raw_body)):
        plan.parameters["time_clarification"] = SlotValue({
            "when": match.group("when"), "body": raw_body[:match.start()].strip(),
        }, "unresolved")


def language_question(plan: TaskPlan) -> str:
    if plan.parameters.get("content_clarification"):
        return ("That wording could be an instruction to look up or compose content. "
                "What exact words should I send? Put them in quotes, or say “use those exact words” "
                "to use the wording you already gave. Nothing has been sent.")
    if slot := plan.parameters.get("time_clarification"):
        return (f"Does “{slot.value['when']}” mean when to send, or is it part of the message? "
                "Say “send then” or “part of the message.” Nothing has been sent.")
    return ""


def answer_language_question(plan: TaskPlan, prompt: str, *, now) -> bool:
    from service.tasks.compiler import _clean_body, _scheduled_at
    reply = prompt.strip().rstrip(".! ").casefold()
    if plan.parameters.get("content_clarification"):
        if quoted(prompt):
            plan.subject = SlotValue(_clean_body(prompt), "followup", original=prompt)
            plan.parameters.pop("time_clarification", None)
        elif reply not in {"use those exact words", "use these exact words", "send those exact words"}:
            return False
        else:
            # Exact wording explicitly includes any trailing time.
            plan.parameters.pop("time_clarification", None)
        plan.parameters.pop("content_clarification")
    elif slot := plan.parameters.get("time_clarification"):
        if reply in {"part of the message", "it's part of the message", "in the message"}:
            pass
        elif reply in {"send then", "when to send", "delivery time"}:
            if plan.intent == "email.reply":
                # Scheduling a reply is not implemented. Keep the clarification
                # pending rather than creating an immediate native reply.
                return False
            plan.subject = SlotValue(_clean_body(slot.value["body"]), "followup", original=prompt)
            plan.parameters["schedule_requested"] = SlotValue(slot.value["when"], "followup")
            plan.temporal.original = slot.value["when"]
            plan.temporal.absolute_iso = _scheduled_at(slot.value["when"], now=now)
            plan.temporal.source = "followup"
        else:
            return False
        plan.parameters.pop("time_clarification")
    else:
        return False
    plan.parameters.pop("reply_args", None)
    return True
