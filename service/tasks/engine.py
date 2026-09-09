"""Persistent coordinator for typed reminder tasks."""
from __future__ import annotations

from datetime import datetime, timedelta
import re
import time

from service.tasks.compiler import compile_task
from service.tasks.models import SlotValue, TaskPlan, TaskTurn, TemporalValue
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
    if plan.intent == "reminder.create" or plan.missing_slots:
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
                      persist: bool = True, now: datetime | None = None) -> TaskTurn | None:
    """Create or advance one typed task without consulting a model."""
    started = time.perf_counter()
    now = now or datetime.now()
    active_raw = store.active_task(sid) if persist else None
    active = TaskPlan.from_dict(active_raw) if active_raw else None
    new_plan = compile_task(prompt, now=now)

    # A pending reminder clarification must not consume an explicit request in
    # another domain.  This happened after an ambiguous "12pm" correction:
    # "check my email for purchases from PlayStation" was interpreted as the
    # title of the reminder to update, and the mail read never reached routing.
    # Leave the reminder pending (the user may return to it later), but let the
    # downstream workflow/read compilers handle this turn.
    unrelated_prompt = prompt.strip().strip("*_` ")
    if (new_plan is None and active is not None
            and active.status in {"waiting_for_input", "failed"}
            and _UNRELATED_SUBJECT_REPLY.search(unrelated_prompt)):
        return None

    if new_plan is None and active is None:
        new_plan = _contextual_update(
            store, sid, prompt, now=now, persist=persist)

    if new_plan is not None:
        if active is not None:
            active.status = "superseded"
            active.updated_at = time.time()
            if persist:
                _save(store, sid, active, "superseded", {"by": new_plan.id})
        plan = new_plan
        event = "task_compiled"
    elif active is None:
        return None
    else:
        plan = active
        if _CANCEL.match(prompt):
            plan.status = "cancelled"
            plan.updated_at = time.time()
            if persist:
                _save(store, sid, plan, "cancelled", {"reply": prompt})
            return _turn(plan, "Okay, I cancelled that reminder request.", "cancelled",
                         started=started)
        if plan.status == "running":
            if _RETRY.match(prompt) or compile_task(prompt, now=now):
                return _turn(
                    plan, "That reminder request is already running. I won’t create a duplicate.",
                    "duplicate_blocked", started=started)
            return None
        if plan.status not in {"waiting_for_input", "failed"}:
            return None

        changed = False
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
        if "subject" in plan.missing_slots:
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
            plan, "I couldn’t safely construct that reminder, so nothing was created.",
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
    _save(store, sid, plan, status, {"result": result[:500], "revision": plan.revision})
