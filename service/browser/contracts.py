"""Closed, versioned browser wire schema. No operational browser implementation.

SCHEMA is the canonical wire description mirrored in Swift and JavaScript.
All keys are required, including nullable keys. Unknown fields fail closed.
Times are UTC Unix milliseconds (safe JSON integers), never inferred time zones.
Validation establishes payload shape/invariants, NOT permission or approval trust.
"""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

if TYPE_CHECKING:
    from service.discovery.contracts import ActionReceipt

VERSION = "1.0"
CAPABILITIES = ["dom_text", "exact_app_approval", "verified_receipts", "local_discovery"]
COMMANDS = ["snapshot", "navigate", "click", "fill", "select", "scroll", "back", "wait", "open_tab", "handoff"]
ERRORS = ["incompatible_version", "missing_capability", "invalid_payload", "disabled",
          "site_permission_denied", "private_context", "foreground_preempted", "budget_exhausted",
          "no_progress", "unsupported_control", "approval_required", "stale_approval",
          "stale_snapshot", "uncertain_receipt", "bridge_unauthorized", "cancelled"]


def string(maximum: int = 4096, minimum: int = 1, pattern: str | None = None) -> dict:
    result = {"type": "string", "min": minimum, "max": maximum}
    if pattern:
        result["pattern"] = pattern
    return result


def enum(*values: Any) -> dict:
    return {"type": "enum", "values": list(values)}


def integer(minimum: int = 0, maximum: int = 9007199254740991) -> dict:
    return {"type": "integer", "min": minimum, "max": maximum}


def array(item: dict, minimum: int = 0, maximum: int = 256) -> dict:
    return {"type": "array", "item": item, "min": minimum, "max": maximum}


def ref(name: str) -> dict:
    return {"type": "ref", "name": name}


def nullable(spec: dict) -> dict:
    return {"type": "nullable", "item": spec}


ID = string(128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
TEXT = string(32768, 0)
URL = string(4096, pattern=r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?(#[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$")
ORIGIN = string(512, pattern=r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
BOOL = {"type": "boolean"}


def record(**fields: dict) -> dict:
    return {"type": "object", "fields": fields}


def versioned(**fields: dict) -> dict:
    return record(schema_version=enum(VERSION), **fields)


# Discovery records are in this shared schema so a consumer has one closed registry.
SCHEMA = {
    "NegotiatedCapabilities": versioned(capabilities=array(enum(*CAPABILITIES), 0, 16)),
    "Handshake": versioned(peer=enum("app", "service", "extension"),
        supported_versions=array(string(16), 1, 16), capabilities=array(enum(*CAPABILITIES), 0, 16),
        required_capabilities=array(enum(*CAPABILITIES), 0, 16),
        credential_role=enum("native_bridge", "app_approval")),
    "ContractError": versioned(code=enum(*ERRORS), message=string(1024), retryable=BOOL),
    "SourceObservation": versioned(id=ID, source_kind=enum("browser", "mail", "calendar", "reminder", "manual"),
        source_url=nullable(URL), source_record_id=nullable(ID), revision=ID,
        observed_at_ms=integer(), title=string(512), text=TEXT, private_context=enum(False)),
    "Evidence": record(id=ID, observation_id=ID, source_revision=ID, quote=string(8192), captured_at_ms=integer()),
    "BrowserElement": record(target_id=ID, role=string(64), label=string(512, 0), editable=BOOL),
    "BrowserSnapshot": versioned(id=ID, task_id=ID, observation_id=ID, url=URL, origin=ORIGIN,
        captured_at_ms=integer(), enabled=enum(True), site_permission=enum("granted"),
        private_context=enum(False), tab_role=enum("background"), content_mode=enum("dom_text"),
        elements=array(ref("BrowserElement"))),
    "ActionIntent": record(action_id=ID, task_id=ID, snapshot_id=ID, command=enum(*COMMANDS),
        target_id=nullable(ID), url=nullable(URL), text=nullable(TEXT),
        private_data=BOOL, consequential=BOOL),
    "ExactApproval": record(proposal_id=ID, authority=enum("app"), approved_at_ms=integer(),
        expires_at_ms=integer(), intent=ref("ActionIntent")),
    "BrowserAction": versioned(intent=ref("ActionIntent"), approval=nullable(ref("ExactApproval"))),
    "InvestigationPolicy": record(explicit_enablement=enum(True), site_permission_required=enum(True),
        exclude_private_at_capture=enum(True), exclude_private_at_service=enum(True),
        separate_background_tab=enum(True), max_proactive_tasks=enum(1), foreground_priority=enum(True),
        max_actions=enum(25), max_active_ms=enum(300000), user_wait_counts=enum(False),
        max_no_progress=enum(3), model_location=enum("local"), model_replaceable=enum(True),
        observation_mode=enum("dom_text"), unsupported_visual=enum("handoff"),
        arbitrary_javascript=enum(False), bridge_credential_role=enum("native_bridge"),
        approval_authority=enum("app")),
    "BrowserTask": versioned(id=ID, item_id=nullable(ID), state=enum("queued", "running", "waiting_approval", "waiting_user",
        "paused_foreground", "handed_off", "succeeded", "failed", "cancelled", "outcome_unknown"),
        policy=ref("InvestigationPolicy"), actions_used=integer(0, 25), active_ms=integer(0, 300000),
        consecutive_no_progress=integer(0, 3), error=nullable(ref("ContractError"))),
    "ExternalRecord": versioned(id=ID, system=enum("browser", "calendar", "reminder", "mail"),
        external_id=ID, url=nullable(URL), revision=ID, evidence_ids=array(ID, 1)),
    "ScheduledBlock": versioned(id=ID, obligation_id=ID, start_ms=integer(), end_ms=integer(),
        timezone=string(128), state=enum("proposed", "scheduled", "cancelled"), external_record_id=nullable(ID)),
    "ActionableItem": versioned(id=ID, kind=enum("assignment", "exam", "scheduling", "follow_up"),
        title=string(512), state=enum("candidate", "needs_clarification", "tracked", "completed", "dismissed"),
        revision=integer(1), supersedes_revision=nullable(integer(1)), due_at_ms=nullable(integer()),
        due_timezone=nullable(string(128)), ambiguity=nullable(string(2048)),
        evidence=array(ref("Evidence"), 1), external_record_ids=array(ID), completion_receipt_id=nullable(ID)),
    "ActionProposal": versioned(id=ID, item_id=ID, item_revision=integer(1), intent=ref("ActionIntent"),
        rationale=string(2048), evidence_ids=array(ID, 1), state=enum("proposed", "approved", "rejected", "expired")),
    "ActionReceipt": versioned(id=ID, action_id=ID, task_id=ID, proposal_id=nullable(ID),
        status=enum("verified", "uncertain", "failed", "cancelled"), recorded_at_ms=integer(),
        evidence=array(ref("Evidence")), external_record_id=nullable(ID),
        completes_obligation=BOOL, error=nullable(ref("ContractError"))),
}


class ContractViolation(ValueError):
    def __init__(self, message: str, code: str = "invalid_payload"):
        super().__init__(message)
        self.code = code


def _check(spec: dict, value: Any, path: str) -> None:
    import re
    kind = spec["type"]
    if kind == "ref":
        validate(spec["name"], value)
        return
    if kind == "nullable":
        if value is not None:
            _check(spec["item"], value, path)
        return
    valid = False
    if kind == "enum":
        valid = any((type(value) is type(candidate) or (type(value) in (int, float) and type(candidate) in (int, float))) and value == candidate for candidate in spec["values"])
    elif kind == "string":
        valid = isinstance(value, str) and spec["min"] <= len(value) <= spec["max"]
        valid = valid and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)
        valid = valid and ("pattern" not in spec or re.fullmatch(spec["pattern"], value) is not None)
    elif kind == "boolean":
        valid = type(value) is bool
    elif kind == "integer":
        valid = type(value) in (int, float) and spec["min"] <= value <= spec["max"] and int(value) == value
    elif kind == "array":
        valid = isinstance(value, list) and spec["min"] <= len(value) <= spec["max"]
        if valid:
            for entry in value:
                _check(spec["item"], entry, path + "[]")
    elif kind == "object":
        valid = isinstance(value, dict) and value.keys() == spec["fields"].keys()
        if valid:
            for key, child in spec["fields"].items():
                _check(child, value[key], path + "." + key)
    if not valid:
        raise ContractViolation(f"Invalid {path}")


def require(condition: bool, message: str, code: str = "invalid_payload") -> None:
    if not condition:
        raise ContractViolation(message, code)


def validate(name: str, payload: Any) -> dict:
    """Return an isolated validated value. It carries no authentication authority."""
    require(name in SCHEMA, "Unknown contract")
    if isinstance(payload, dict) and "schema_version" in payload:
        require(payload["schema_version"] == VERSION, "Unsupported schema version", "incompatible_version")
    _check(SCHEMA[name], payload, name)
    p = payload
    if name == "Handshake":
        require(VERSION in p["supported_versions"], "No compatible version", "incompatible_version")
        for key in ("supported_versions", "capabilities", "required_capabilities"):
            require(len(set(p[key])) == len(p[key]), "Duplicate negotiation entry")
        require(p["peer"] == "app" or p["credential_role"] == "native_bridge", "Bridge cannot grant app authority")
    elif name == "SourceObservation":
        require(p["source_kind"] != "browser" or p["source_url"] is not None, "Browser source needs URL")
    elif name == "BrowserSnapshot":
        from urllib.parse import urlsplit
        u = urlsplit(p["url"])
        require(p["origin"] == f"{u.scheme}://{u.netloc}", "Snapshot origin mismatch")
        ids = [e["target_id"] for e in p["elements"]]
        require(len(set(ids)) == len(ids), "Duplicate target")
    elif name == "ActionIntent":
        c = p["command"]
        require((p["target_id"] is not None) == (c in ("click", "fill", "select")), "Target/command mismatch")
        require((p["url"] is not None) == (c in ("navigate", "open_tab")), "URL/command mismatch")
        require((p["text"] is not None) == (c in ("fill", "select")), "Text/command mismatch")
        require(not p["private_data"] or c == "fill", "Private data requires typing")
    elif name == "ExactApproval":
        require(p["expires_at_ms"] > p["approved_at_ms"], "Invalid approval lifetime")
    elif name == "BrowserAction":
        i, a = p["intent"], p["approval"]
        needs = i["consequential"] or i["private_data"] or i["command"] in ("click", "fill", "select")
        require(not needs or a is not None, "Exact app approval required", "approval_required")
        require(a is None or a["intent"] == i, "Approval intent mismatch", "stale_approval")
    elif name == "BrowserTask":
        exhausted = p["actions_used"] == 25 or p["active_ms"] == 300000 or p["consecutive_no_progress"] == 3
        require(not exhausted or p["state"] not in ("queued", "running"), "Exhausted task cannot run")
    elif name == "ScheduledBlock":
        require(p["end_ms"] > p["start_ms"], "Invalid block interval")
    elif name == "ActionableItem":
        require((p["due_at_ms"] is None) == (p["due_timezone"] is None), "Deadline timezone required")
        require(p["supersedes_revision"] is None or p["supersedes_revision"] < p["revision"], "Invalid revision chain")
        require(p["state"] != "needs_clarification" or p["ambiguity"] is not None, "Missing ambiguity")
        require(p["ambiguity"] is None or p["state"] in ("candidate", "needs_clarification", "dismissed"), "Unresolved item")
        require((p["completion_receipt_id"] is not None) == (p["state"] == "completed"), "Completion needs receipt")
    elif name == "ActionReceipt":
        require(p["status"] != "verified" or bool(p["evidence"]), "Verification needs evidence")
        require(not p["completes_obligation"] or p["status"] == "verified", "Uncertain receipt cannot complete obligation")
        require(p["status"] != "verified" or p["error"] is None, "Verified receipt cannot carry error")
    return deepcopy(payload)


def negotiate(local: dict, remote: dict) -> dict:
    """Symmetric capability agreement; never authenticates either peer."""
    a, b = validate("Handshake", local), validate("Handshake", remote)
    common = sorted(set(a["capabilities"]) & set(b["capabilities"]))
    required = set(a["required_capabilities"]) | set(b["required_capabilities"])
    require(required <= set(common), "Required capability unavailable", "missing_capability")
    return {"schema_version": VERSION, "capabilities": common}


# Wire aliases are expanded to TypedDicts below; schema validation is mandatory at boundaries.
class SourceObservation(TypedDict):
    schema_version: str
    id: str
    source_kind: Literal["browser", "mail", "calendar", "reminder", "manual"]
    source_url: str | None
    source_record_id: str | None
    revision: str
    observed_at_ms: int
    title: str
    text: str
    private_context: bool


class BrowserSnapshot(TypedDict):
    schema_version: str
    id: str
    task_id: str
    observation_id: str
    url: str
    origin: str
    captured_at_ms: int
    enabled: bool
    site_permission: str
    private_context: bool
    tab_role: str
    content_mode: str
    elements: list[dict]


class BrowserAction(TypedDict):
    schema_version: str
    intent: dict
    approval: dict | None


class BrowserTask(TypedDict):
    schema_version: str
    id: str
    item_id: str | None
    state: str
    policy: dict
    actions_used: int
    active_ms: int
    consecutive_no_progress: int
    error: dict | None


class BrowserService(Protocol):
    """Implementations recheck live permissions, budgets, and exact app approval.

    Bridge credentials identify transport only. Never accept an approval object
    from an extension as proof of user consent. Resolve it in the app's authority.
    """
    async def negotiate(self, peer: dict) -> dict: ...
    async def snapshot(self, task_id: str) -> BrowserSnapshot: ...
    async def execute(self, action: BrowserAction) -> "ActionReceipt": ...
    async def cancel(self, task_id: str) -> BrowserTask: ...
