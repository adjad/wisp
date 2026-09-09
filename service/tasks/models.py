"""Serializable task plans used between language and tool execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
import uuid
from typing import Any


SCHEMA_VERSION = 1
ACTIVE_TASK_STATUSES = {
    "waiting_for_input", "ready", "running", "failed",
}
# Intents that send something to a person.  They share one recipient slot and
# one resolution path, so the invariants below apply to all of them.
OUTBOUND_INTENTS = frozenset({"message.send", "email.send", "email.reply"})
CHANNEL_FOR_INTENT = {"message.send": "messages", "email.send": "email", "email.reply": "email"}


@dataclass
class SlotValue:
    value: Any = None
    source: str = ""  # explicit | followup | default | resolved
    turn: int = 0
    confidence: float = 1.0
    original: str = ""

    @classmethod
    def from_dict(cls, value: dict | None) -> "SlotValue":
        if not isinstance(value, dict):
            return cls(value=value)
        names = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in names})


@dataclass
class TemporalValue:
    original: str = ""
    absolute_iso: str = ""
    reference: str = ""
    reference_id: str = ""
    reference_when_iso: str = ""
    lead_seconds: int | None = None
    timezone: str = ""
    source: str = ""
    defaulted_part_of_day: str = ""

    @classmethod
    def from_dict(cls, value: dict | None) -> "TemporalValue":
        names = cls.__dataclass_fields__
        return cls(**{key: item for key, item in (value or {}).items() if key in names})


@dataclass
class StepPlan:
    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    max_calls: int = 1
    effect: bool = False
    success_statuses: list[str] = field(default_factory=lambda: ["succeeded"])

    @classmethod
    def from_dict(cls, value: dict) -> "StepPlan":
        names = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in names})


@dataclass
class TaskPlan:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = SCHEMA_VERSION
    kind: str = "task.reminder.create"
    intent: str = "reminder.create"
    original_request: str = ""
    owner: SlotValue = field(default_factory=lambda: SlotValue("user", "default"))
    subject: SlotValue = field(default_factory=SlotValue)
    target: SlotValue = field(default_factory=SlotValue)
    recipient: SlotValue | None = None
    channel: SlotValue = field(
        default_factory=lambda: SlotValue("reminders", "default"))
    temporal: TemporalValue = field(default_factory=TemporalValue)
    parameters: dict[str, SlotValue] = field(default_factory=dict)
    resolved_targets: list[dict[str, Any]] = field(default_factory=list)
    # Snapshot of the contact lookup that grounded `recipient`, written by the
    # engine between compile and plan.  The planner copies its address into
    # step args, so approval binds to an exact destination.  Any edit to
    # `recipient` or `channel` must null this and bump `revision`.
    resolved_recipient: dict[str, Any] | None = None
    resolved_references: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)
    steps: list[StepPlan] = field(default_factory=list)
    # Effect call ids already attempted. Written BEFORE the effect runs and
    # persisted by the caller, so a crash mid-send leaves evidence that the
    # attempt happened rather than an invitation to repeat it.
    claimed_calls: list[str] = field(default_factory=list)
    status: str = "ready"
    revision: int = 1
    idempotency_key: str = field(default_factory=lambda: uuid.uuid4().hex)
    last_error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def recompute_status(self) -> str:
        missing: list[str] = []
        if self.intent == "reminder.create":
            if not str(self.subject.value or "").strip():
                missing.append("subject")
            if not self.temporal.absolute_iso:
                if self.temporal.reference and self.temporal.lead_seconds is None:
                    missing.append("temporal.lead_time")
                elif not self.temporal.reference:
                    missing.append("temporal.time")
        elif self.intent == "reminder.update":
            if not str(self.target.value or "").strip():
                missing.append("target")
            has_change = bool(
                self.temporal.absolute_iso
                or str(self.parameters.get("day", SlotValue()).value or "").strip()
                or str(self.parameters.get("new_title", SlotValue()).value or "").strip()
            )
            if not has_change:
                missing.append("update.change")
        elif self.intent == "reminder.complete":
            if not str(self.target.value or "").strip():
                missing.append("target")
        elif self.intent == "reminder.delete":
            if not str(self.parameters.get("scope", SlotValue()).value or "").strip():
                missing.append("scope")
        elif self.intent == "email.reply":
            if not str(self.subject.value or "").strip():
                missing.append("subject")
            if not self.resolved_references.get("reply.target"):
                missing.append("reply.target")
            if not self.parameters.get("reply_args"):
                missing.append("reply.envelope")
        elif self.intent in OUTBOUND_INTENTS:
            if not str(self.subject.value or "").strip():
                missing.append("subject")
            # A requested-but-unresolved send time is NOT "send now".
            if (str(self.parameters.get(
                    "schedule_requested", SlotValue()).value or "").strip()
                    and not self.temporal.absolute_iso):
                missing.append("temporal.time")
            if self.recipient is None or not str(self.recipient.value or "").strip():
                missing.append("recipient")
            if (self.intent == "email.send"
                    and not str(self.parameters.get(
                        "email_subject", SlotValue()).value or "").strip()):
                missing.append("email.subject")
        else:
            missing.append("intent")
        if self.intent in OUTBOUND_INTENTS:
            if self.parameters.get("content_clarification"):
                missing.append("content.mode")
            if self.parameters.get("time_clarification"):
                missing.append("temporal.interpretation")
        self.missing_slots = missing
        self.status = "waiting_for_input" if missing else "ready"
        self.updated_at = time.time()
        return self.status

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "TaskPlan":
        names = cls.__dataclass_fields__
        kwargs = {key: item for key, item in value.items() if key in names}
        kwargs["owner"] = SlotValue.from_dict(kwargs.get("owner"))
        kwargs["subject"] = SlotValue.from_dict(kwargs.get("subject"))
        kwargs["target"] = SlotValue.from_dict(kwargs.get("target"))
        if kwargs.get("recipient") is not None:
            kwargs["recipient"] = SlotValue.from_dict(kwargs["recipient"])
        kwargs["channel"] = SlotValue.from_dict(kwargs.get("channel"))
        kwargs["temporal"] = TemporalValue.from_dict(kwargs.get("temporal"))
        kwargs["parameters"] = {
            str(key): SlotValue.from_dict(item)
            for key, item in (kwargs.get("parameters") or {}).items()
        }
        kwargs["steps"] = [StepPlan.from_dict(item) for item in kwargs.get("steps", [])]
        return cls(**kwargs)


@dataclass
class TaskTurn:
    plan: TaskPlan
    response: str = ""
    event: str = ""
    executable: bool = False
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskExecution:
    status: str
    response: str
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
