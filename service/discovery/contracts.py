"""Discovery interfaces. Obligations are not scheduled blocks or external records."""
from __future__ import annotations

from typing import Protocol, TypedDict
from service.browser.contracts import SourceObservation, require, validate


class ActionableItem(TypedDict):
    schema_version: str
    id: str
    kind: str
    title: str
    state: str
    revision: int
    supersedes_revision: int | None
    due_at_ms: int | None
    due_timezone: str | None
    ambiguity: str | None
    evidence: list[dict]
    external_record_ids: list[str]
    completion_receipt_id: str | None


class ActionProposal(TypedDict):
    schema_version: str
    id: str
    item_id: str
    item_revision: int
    intent: dict
    rationale: str
    evidence_ids: list[str]
    state: str


class ActionReceipt(TypedDict):
    schema_version: str
    id: str
    action_id: str
    task_id: str
    proposal_id: str | None
    status: str
    recorded_at_ms: int
    evidence: list[dict]
    external_record_id: str | None
    completes_obligation: bool
    error: dict | None


class ScheduledBlock(TypedDict):
    schema_version: str
    id: str
    obligation_id: str
    start_ms: int
    end_ms: int
    timezone: str
    state: str
    external_record_id: str | None


class ExternalRecord(TypedDict):
    schema_version: str
    id: str
    system: str
    external_id: str
    url: str | None
    revision: str
    evidence_ids: list[str]


def validate_proposal(item: dict, proposal: dict) -> None:
    i, p = validate("ActionableItem", item), validate("ActionProposal", proposal)
    require(p["item_id"] == i["id"] and p["item_revision"] == i["revision"], "Stale proposal")
    require(set(p["evidence_ids"]) <= {e["id"] for e in i["evidence"]}, "Ungrounded proposal")
    require(i["state"] == "tracked", "Proposal requires a resolved tracked obligation")


def validate_completion(item: dict, receipt: dict, proposal: dict) -> None:
    """Relationship check only. Evidence authenticity still requires verification."""
    i, r, p = (validate(n, v) for n, v in
               (("ActionableItem", item), ("ActionReceipt", receipt), ("ActionProposal", proposal)))
    require(i["state"] == "completed" and i["completion_receipt_id"] == r["id"], "Receipt link mismatch")
    require(r["status"] == "verified" and r["completes_obligation"], "Receipt does not prove completion")
    require(r["proposal_id"] == p["id"] and r["action_id"] == p["intent"]["action_id"]
            and r["task_id"] == p["intent"]["task_id"], "Receipt action mismatch")
    require(p["item_id"] == i["id"] and p["item_revision"] == i["revision"], "Receipt item mismatch")
    require(p["state"] == "approved", "Completion requires approved proposal")
    require(set(p["evidence_ids"]) <= {e["id"] for e in i["evidence"]}, "Ungrounded completion proposal")


class DiscoveryService(Protocol):
    """Local replaceable extraction; incomplete coverage never means deletion."""
    async def extract(self, observation: SourceObservation) -> list[ActionableItem]: ...
    async def reconcile(self, items: list[ActionableItem]) -> list[ActionableItem]: ...
    async def propose(self, item_id: str, revision: int) -> ActionProposal: ...
    async def reconcile_receipt(self, receipt: ActionReceipt) -> ActionableItem: ...
