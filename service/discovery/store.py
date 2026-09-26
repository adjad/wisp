"""Additive, local A01 record persistence on the assistant database.

Storage revisions are CAS tokens, distinct from source/item wire revisions.
Observations, evidence and receipts are immutable; a changed source capture gets
another observation ID. No read or write here constitutes approval authority,
source authenticity, runtime ingestion, or permission to perform an effect.
"""
from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from service.browser.contracts import require, validate
from service.discovery.contracts import validate_completion, validate_proposal

if TYPE_CHECKING:
    from service.assistant.store import AssistantStore

TABLES = {
    "SourceObservation": "discovery_observations",
    "Evidence": "discovery_evidence",
    "ActionableItem": "discovery_items",
    "ActionProposal": "discovery_proposals",
    "BrowserTask": "discovery_browser_tasks",
    "ActionReceipt": "discovery_receipts",
    "ScheduledBlock": "discovery_blocks",
    "ExternalRecord": "discovery_external_records",
}
IMMUTABLE = frozenset({"SourceObservation", "Evidence", "ActionReceipt"})


class RevisionConflict(ValueError):
    """The caller's storage snapshot is no longer current."""


def integer(value: int, name: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise ValueError(f"Invalid {name}")
    return value


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def migrate(db: sqlite3.Connection) -> None:
    """Called only inside AssistantStore's atomic startup migration."""
    for table in TABLES.values():
        db.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            id TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
            payload TEXT NOT NULL, overrides TEXT NOT NULL DEFAULT '{{}}'
        )""")
    db.execute("""CREATE TABLE IF NOT EXISTS discovery_record_history (
        kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
        payload TEXT NOT NULL, overrides TEXT NOT NULL,
        PRIMARY KEY(kind, id, revision)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_discovery_observation_source ON discovery_observations "
               "(json_extract(payload,'$.source_kind'), json_extract(payload,'$.source_record_id'), "
               "json_extract(payload,'$.revision'))")
    db.execute("CREATE INDEX IF NOT EXISTS idx_discovery_item_state ON discovery_items "
               "(json_extract(payload,'$.state'), json_extract(payload,'$.due_at_ms'))")
    db.execute("CREATE INDEX IF NOT EXISTS idx_discovery_proposal_item ON discovery_proposals "
               "(json_extract(payload,'$.item_id'), json_extract(payload,'$.item_revision'))")
    db.execute("""CREATE TABLE IF NOT EXISTS discovery_jobs (
        id TEXT PRIMARY KEY NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','running','waiting_approval','waiting_user',
            'succeeded','failed','cancelled','outcome_unknown')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL, available_at_ms INTEGER NOT NULL,
        claim_owner TEXT, claim_token TEXT, lease_until_ms INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
        effect_pending INTEGER NOT NULL DEFAULT 0,
        actions_used INTEGER NOT NULL DEFAULT 0 CHECK(actions_used BETWEEN 0 AND 25),
        active_ms INTEGER NOT NULL DEFAULT 0 CHECK(active_ms BETWEEN 0 AND 300000),
        consecutive_no_progress INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_no_progress BETWEEN 0 AND 3),
        result TEXT,
        CHECK((state='running' AND claim_owner IS NOT NULL AND claim_token IS NOT NULL AND lease_until_ms IS NOT NULL)
           OR (state<>'running' AND claim_owner IS NULL AND claim_token IS NULL AND lease_until_ms IS NULL))
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_discovery_job_queue ON discovery_jobs(state, available_at_ms, created_at_ms, id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_discovery_job_lease ON discovery_jobs(state, lease_until_ms)")
    # Decisions and claims are separate append-only facts. Migration deliberately
    # does not promote legacy proposal state='approved' into consent.
    db.execute("""CREATE TABLE IF NOT EXISTS discovery_approval_decisions (
        proposal_id TEXT PRIMARY KEY NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
        proposal_revision INTEGER NOT NULL,
        item_id TEXT NOT NULL, item_revision INTEGER NOT NULL,
        item_storage_revision INTEGER NOT NULL,
        intent TEXT NOT NULL, evidence_ids TEXT NOT NULL,
        decided_at_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL,
        CHECK(expires_at_ms > decided_at_ms)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS discovery_approval_consumptions (
        proposal_id TEXT PRIMARY KEY NOT NULL,
        task_id TEXT NOT NULL, action_id TEXT NOT NULL,
        consumed_at_ms INTEGER NOT NULL,
        UNIQUE(task_id, action_id),
        FOREIGN KEY(proposal_id) REFERENCES discovery_approval_decisions(proposal_id)
    )""")
    for table in ('discovery_approval_decisions', 'discovery_approval_consumptions',
                  'discovery_record_history'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN
                SELECT RAISE(ABORT, 'Discovery history is immutable'); END""")


def _table(kind: str) -> str:
    if kind not in TABLES:
        raise ValueError("Unsupported discovery record")
    return TABLES[kind]


def _record(row) -> dict | None:
    return None if row is None else dict(payload=json.loads(row["payload"]),
        revision=row["revision"], overrides=json.loads(row["overrides"]))


class DiscoveryStore:
    """Typed payloads with explicit create (revision=0) and update CAS.

    `get` returns {payload, revision, overrides}. Overrides are separate local
    metadata, preserved verbatim by ordinary saves; only set_overrides changes
    them. They are not silently merged into wire payloads or Today records.
    Compose multiple saves and job operations with assistant.transaction().
    """
    def __init__(self, assistant: AssistantStore):
        self.assistant = assistant

    def get(self, kind: str, record_id: str, *, revision: int | None = None) -> dict | None:
        table = _table(kind)
        with self.assistant.transaction(write=False) as db:
            if revision is None:
                row = db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            else:
                integer(revision, "revision", 1)
                row = db.execute("SELECT * FROM discovery_record_history WHERE kind=? AND id=? AND revision=?",
                                 (kind, record_id, revision)).fetchone()
            return _record(row)

    def save(self, kind: str, payload: dict, *, expected_revision: int = 0) -> dict:
        table = _table(kind)
        integer(expected_revision, "expected_revision")
        value = validate(kind, payload)
        with self.assistant.transaction() as db:
            old = _record(db.execute(f"SELECT * FROM {table} WHERE id=?", (value["id"],)).fetchone())
            if kind in IMMUTABLE and old and old["payload"] == value:
                # Exact retry of an immutable fact is harmless at any revision.
                return old
            if (old["revision"] if old else 0) != expected_revision:
                raise RevisionConflict("Discovery record changed")
            if old and kind in IMMUTABLE:
                raise RevisionConflict("Captured records are immutable; use a new ID")
            self._relationships(kind, value, old)
            revision = expected_revision + 1
            overrides = encode(old["overrides"] if old else {})
            encoded = encode(value)
            db.execute(f"INSERT INTO {table}(id,revision,payload,overrides) VALUES (?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                       (value["id"], revision, encoded, overrides))
            db.execute("INSERT INTO discovery_record_history VALUES (?,?,?,?,?)",
                       (kind, value["id"], revision, encoded, overrides))
            return dict(payload=value, revision=revision, overrides=json.loads(overrides))

    def _relationships(self, kind: str, value: dict, old: dict | None) -> None:
        if kind == "Evidence":
            source = self.get("SourceObservation", value["observation_id"])
            require(source is not None, "Missing observation")
            source = source["payload"]
            require(value["source_revision"] == source["revision"], "Evidence revision mismatch")
            require(value["captured_at_ms"] == source["observed_at_ms"], "Evidence capture mismatch")
            require(value["quote"] in source["text"], "Evidence quote missing from observation")
        for evidence in value.get("evidence", []):
            stored = self.get("Evidence", evidence["id"])
            require(stored is not None and stored["payload"] == evidence, "Missing or changed evidence")
        for evidence_id in value.get("evidence_ids", []):
            require(self.get("Evidence", evidence_id) is not None, "Missing evidence")
        if kind == "ActionProposal":
            if old:
                require({k: v for k, v in old["payload"].items() if k != "state"} ==
                        {k: v for k, v in value.items() if k != "state"}, "Proposal intent is immutable")
                require(old["payload"]["state"] not in {"expired", "rejected"} or
                        value["state"] in {"expired", "rejected"}, "Retired proposal cannot be reactivated")
            # Retiring a stored proposal must work after its obligation changes.
            # This removes authority only; creating/reactivating a proposal still
            # requires a current tracked item and exact evidence relationship.
            retiring = old is not None and value["state"] in {"expired", "rejected"}
            if not retiring:
                item = self.get("ActionableItem", value["item_id"])
                require(item is not None, "Missing obligation")
                validate_proposal(item["payload"], value)
            if value['state'] == 'approved':
                # Only ApprovalStore's same-transaction decision can introduce
                # this state. A source/model-provided state never grants consent.
                with self.assistant.transaction(write=False) as db:
                    decision = db.execute("SELECT * FROM discovery_approval_decisions WHERE proposal_id=?",
                                          (value['id'],)).fetchone()
                require(old is not None and old['payload']['state'] == 'proposed' and
                        decision is not None and decision['decision'] == 'approved' and
                        decision['proposal_revision'] == old['revision'] + 1,
                        'Approved state requires a trusted app decision')
        if kind == "ActionableItem":
            if old:
                previous = old["payload"]
                content = lambda p: {k: v for k, v in p.items() if k not in
                                     {"state", "completion_receipt_id", "revision", "supersedes_revision"}}
                if value["revision"] == previous["revision"]:
                    # Reopening must invalidate prior approvals and completion
                    # receipts, whose authority is bound to the wire revision.
                    require(previous["state"] not in {"completed", "dismissed"} or
                            value["state"] == previous["state"], "Leaving a terminal item needs a new revision")
                    require(content(value) == content(previous) and
                            value["supersedes_revision"] == previous["supersedes_revision"], "Content changes need a new item revision")
                else:
                    require(value["revision"] == previous["revision"] + 1 and
                            value["supersedes_revision"] == previous["revision"], "Invalid item revision chain")
            if value["state"] == "completed":
                receipt = self.get("ActionReceipt", value["completion_receipt_id"])
                require(receipt is not None, "Missing completion receipt")
                proposal = self.completion_proposal(receipt["payload"]["id"])
                validate_completion(value, receipt["payload"], proposal["payload"])
        if kind == "BrowserTask" and old:
            require(old["payload"]["consecutive_no_progress"] < 3 or
                    value["consecutive_no_progress"] == 3, "Exhausted no-progress budget cannot reset")
            for field in ("actions_used", "active_ms"):
                require(value[field] >= old["payload"][field], "Task accounting cannot decrease")
        if kind == "ScheduledBlock":
            require(self.get("ActionableItem", value["obligation_id"]) is not None, "Missing obligation")

    def completion_proposal(self, receipt_id: str) -> dict:
        """Resolve consumed consent against immutable history, even after retirement.

        This proves relationships and consent, not authenticity of an external
        receipt. A future verifier must establish that independently. Missing
        proof (including legacy A02 rows) never proves no external effect occurred;
        callers must reconcile uncertainty rather than infer permission to retry.
        """
        with self.assistant.transaction(write=False) as db:
            receipt = self.get('ActionReceipt', receipt_id)
            require(receipt is not None, 'Missing completion receipt')
            value = receipt['payload']
            row = db.execute("""SELECT d.*, c.consumed_at_ms, c.task_id, c.action_id
                FROM discovery_approval_decisions d JOIN discovery_approval_consumptions c
                ON d.proposal_id=c.proposal_id WHERE d.proposal_id=?""",
                (value['proposal_id'],)).fetchone()
            require(row is not None and row['decision'] == 'approved',
                    'Completion requires consumed app approval')
            require(value['recorded_at_ms'] >= row['consumed_at_ms'] and
                    value['task_id'] == row['task_id'] and value['action_id'] == row['action_id'],
                    'Receipt predates or mismatches consumption')
            proposal = self.get('ActionProposal', row['proposal_id'], revision=row['proposal_revision'])
            require(proposal is not None and proposal['payload']['state'] == 'approved',
                    'Missing approved proposal history')
            return proposal

    def set_overrides(self, item_id: str, overrides: dict, *, expected_revision: int) -> dict:
        """Replace explicit local override metadata, never inferred by save()."""
        integer(expected_revision, "expected_revision", 1)
        if not isinstance(overrides, dict):
            raise ValueError("Overrides must be an object")
        encoded = encode(overrides)
        with self.assistant.transaction() as db:
            current = self.get("ActionableItem", item_id)
            if current is None or current["revision"] != expected_revision:
                raise RevisionConflict("Discovery record changed")
            revision = expected_revision + 1
            db.execute("UPDATE discovery_items SET overrides=?,revision=? WHERE id=?", (encoded, revision, item_id))
            db.execute("INSERT INTO discovery_record_history VALUES (?,?,?,?,?)",
                       ("ActionableItem", item_id, revision, encode(current["payload"]), encoded))
            return dict(payload=current["payload"], revision=revision, overrides=json.loads(encoded))
