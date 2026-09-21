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
    "waiting_for_content",
    "waiting_for_channel", "waiting_for_recipient", "waiting_for_time",
    "waiting_for_location", "waiting_for_symbols",
    "ready", "running", "failed",
}


@dataclass
class WorkflowPlan:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Monotonic persisted identity for this exact workflow state. Executors
    # may act only while the stored row still has this revision and is running.
    revision: int = 0
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
    artifact_request: str = ""
    artifact_provenance: str = ""
    news_artifact_provenance: dict = field(default_factory=dict)
    news_clarification_provenance: dict = field(default_factory=dict)
    content_error: str = ""
    status: str = "ready"
    last_error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def recompute_status(self) -> str:
        if self.content_error or self.news_clarification_provenance:
            self.status = "waiting_for_content"
        elif not self.channel:
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
        value = asdict(self)
        if not value["artifact_provenance"]:
            value.pop("artifact_provenance")
        if not value["news_artifact_provenance"]:
            value.pop("news_artifact_provenance")
        if not value["news_clarification_provenance"]:
            value.pop("news_clarification_provenance")
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "WorkflowPlan":
        if not isinstance(value, dict):
            raise ValueError("Workflow state must be an object")
        names = cls.__dataclass_fields__
        plan = cls(**{key: val for key, val in value.items() if key in names})
        if (value.get("status") == "waiting_for_content"
                and "news_clarification_provenance" in value
                and not value["news_clarification_provenance"]):
            raise ValueError("Stored-news clarification proof is empty")
        legacy_provenance = plan.artifact_provenance
        if legacy_provenance and legacy_provenance != "verified_tool_receipt":
            raise ValueError("Unrecognized legacy artifact provenance")

        def valid_news_provenance(provenance) -> bool:
            return (
                isinstance(provenance, dict)
                and {"session_id", "turn_idx", "kind", "sha256"}.issubset(provenance)
                and isinstance(provenance["session_id"], str) and provenance["session_id"]
                and isinstance(provenance["turn_idx"], int)
                and not isinstance(provenance["turn_idx"], bool)
                and provenance["turn_idx"] >= 0
                and provenance["kind"] == "news"
                and isinstance(provenance["sha256"], str)
                and bool(provenance["sha256"])
            )

        artifact_provenance = plan.news_artifact_provenance
        clarification_provenance = plan.news_clarification_provenance
        if artifact_provenance and not valid_news_provenance(artifact_provenance):
            raise ValueError("Invalid stored-news artifact provenance")
        if clarification_provenance and not valid_news_provenance(clarification_provenance):
            raise ValueError("Invalid stored-news clarification provenance")
        if artifact_provenance and clarification_provenance:
            raise ValueError("Workflow cannot bind and clarify the same news artifact")
        if artifact_provenance and (
                not plan.artifact_text or plan.sources or legacy_provenance):
            raise ValueError("Stored-news delivery state is contradictory")
        if clarification_provenance and (
                plan.status not in {"ready", "waiting_for_content"}
                or plan.artifact_text or artifact_provenance or legacy_provenance):
            raise ValueError("Stored-news clarification state is contradictory")
        return plan

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
