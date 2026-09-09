"""Serializable workflow state.

The router decides which tools are plausible for an isolated utterance.  A
workflow records what an unfinished task *means* across turns, including its
source, destination and completion criteria.  Keeping this data typed prevents
short replies such as ``Messages`` or ``yes`` from being reinterpreted as new
calendar/reminder requests.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import time
import uuid


ACTIVE_STATUSES = {
    "waiting_for_channel", "waiting_for_recipient", "waiting_for_time",
    "waiting_for_location", "waiting_for_symbols",
    "ready", "running", "failed",
}


@dataclass
class WorkflowPlan:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = "deliver_summary"
    sources: list[str] = field(default_factory=list)
    source_args: dict[str, dict] = field(default_factory=dict)
    recipient: str = ""
    channel: str = ""               # messages | email
    delivery: str = "send"          # send | draft | scheduled
    when: str = ""
    location: str = ""
    stock_symbols: list[str] = field(default_factory=list)
    date_range: str = ""
    original_request: str = ""
    # Explicitly referenced conversation content is data, never instructions.
    artifact_text: str = ""
    status: str = "ready"
    last_error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def recompute_status(self) -> str:
        if not self.channel:
            self.status = "waiting_for_channel"
        elif not self.recipient:
            self.status = "waiting_for_recipient"
        elif self.delivery == "scheduled" and not self.when:
            self.status = "waiting_for_time"
        elif "weather" in self.sources and not self.location:
            self.status = "waiting_for_location"
        elif "stock" in self.sources and not self.stock_symbols:
            self.status = "waiting_for_symbols"
        else:
            self.status = "ready"
        self.updated_at = time.time()
        return self.status

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "WorkflowPlan":
        names = cls.__dataclass_fields__
        return cls(**{key: val for key, val in value.items() if key in names})

    def prompt_block(self) -> str:
        source_lines = ", ".join(self.sources)
        return (
            "\nAUTHORITATIVE WORKFLOW PLAN (compiled from the user's request):\n"
            f"- workflow_id: {self.id}\n"
            f"- sources: {source_lines}\n"
            f"- requested range: {self.date_range or 'most recent/default'}\n"
            f"- delivery: {self.delivery}\n"
            f"- channel: {self.channel}\n"
            f"- recipient: {self.recipient}\n"
            f"- scheduled time: {self.when or 'none'}\n"
            f"- weather location: {self.location or 'none'}\n"
            f"- stocks: {', '.join(self.stock_symbols) or 'none'}\n"
            f"- exact source arguments: {json.dumps(self.source_args, sort_keys=True)}\n"
            "Use the bound conversation artifact when present; otherwise use "
            "only the successful source results returned in this turn to "
            "write the outbound body. Preserve an empty result as an honest "
            "'nothing found' statement. Do not invent, infer, or add events, "
            "messages, prices, names, dates, or times. Call the one delivery "
            "tool in the plan after all source calls. The ordinary approval "
            "step still applies; never claim delivery before its tool succeeds. "
            "For reminders, Wisp/Apple labels identify the storage app only. "
            "They do not identify a creator or sender. Unless a successful "
            "source result explicitly names a creator, call it 'the user's "
            "reminder' and never attribute it to Mom or another person."
        )


@dataclass
class WorkflowTurn:
    plan: WorkflowPlan
    response: str = ""
    decision: object | None = None
    event: str = ""
