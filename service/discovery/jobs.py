"""Durable bounded work, with cross-connection claims and fenced mutations.

Times are UTC Unix milliseconds supplied by a trusted service clock (or an
injected test clock). A claim is not effect authorization. Future runtimes must
reserve action/time budgets BEFORE work and mark_effect_pending BEFORE dispatch;
marking does not check approval. Waiting time is never charged. Uncertain work
cannot be retried here: reconcile records a terminal result after investigation.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Callable

from service.discovery.store import RevisionConflict, encode, integer

if TYPE_CHECKING:
    from service.assistant.store import AssistantStore

STATES = frozenset({"queued", "running", "waiting_approval", "waiting_user", "succeeded",
                    "failed", "cancelled", "outcome_unknown"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
WAITING = frozenset({"waiting_approval", "waiting_user"})


class ClaimConflict(RevisionConflict):
    """Claim was lost, expired, or belongs to another worker."""


class InvalidTransition(ValueError):
    pass


class BudgetExhausted(ValueError):
    pass


def _identity(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError("Invalid job identity")
    return value


def _decode(row) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    result["result"] = json.loads(result["result"]) if result["result"] is not None else None
    result["effect_pending"] = bool(result["effect_pending"])
    result["cancel_requested"] = bool(result["cancel_requested"])
    return result


def _exhausted(job: dict) -> bool:
    return job["actions_used"] >= 25 or job["active_ms"] >= 300000 or job["consecutive_no_progress"] >= 3


class JobStore:
    def __init__(self, assistant: AssistantStore, *, clock: Callable[[], int] | None = None):
        self.assistant = assistant
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)

    def _now(self) -> int:
        return integer(self.clock(), "clock")

    def get(self, job_id: str) -> dict | None:
        with self.assistant.transaction(write=False) as db:
            return _decode(db.execute("SELECT * FROM discovery_jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue(self, kind: str, payload: dict, *, job_id: str | None = None,
                available_at_ms: int | None = None) -> dict:
        """Idempotent by explicit ID only when kind, payload and schedule match."""
        job_id = _identity(uuid.uuid4().hex if job_id is None else job_id)
        _identity(kind)
        if not isinstance(payload, dict):
            raise ValueError("Job payload must be an object")
        encoded = encode(payload)
        with self.assistant.transaction() as db:
            now = self._now()
            available = now if available_at_ms is None else integer(available_at_ms, "available_at_ms")
            old = self.get(job_id)
            if old:
                if old["kind"] != kind or encode(old["payload"]) != encoded or (
                        available_at_ms is not None and old["available_at_ms"] != available):
                    raise RevisionConflict("Job identity reused with different work")
                return old
            db.execute("INSERT INTO discovery_jobs(id,kind,payload,state,revision,created_at_ms,updated_at_ms,available_at_ms) "
                       "VALUES (?,?,?,'queued',1,?,?,?)", (job_id, kind, encoded, now, now, available))
            return self.get(job_id)

    def _current(self, job_id: str, revision: int) -> dict:
        integer(revision, "expected_revision", 1)
        current = self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        if current["revision"] != revision:
            raise RevisionConflict("Job changed")
        return current

    @staticmethod
    def _claimed(current: dict, token: str, now: int) -> None:
        if (current["state"] != "running" or not token or current["claim_token"] != token
                or current["lease_until_ms"] <= now):
            raise ClaimConflict("Job claim lost or expired")

    def _update(self, db, current: dict, now: int, **changes) -> dict:
        # All callers hold BEGIN IMMEDIATE; the predicate also documents/fences
        # the CAS write if this helper's implementation changes later.
        changes.update(revision=current["revision"] + 1, updated_at_ms=max(now, current["updated_at_ms"]))
        columns = ",".join(f"{key}=?" for key in changes)
        cursor = db.execute(f"UPDATE discovery_jobs SET {columns} WHERE id=? AND revision=?",
                            (*changes.values(), current["id"], current["revision"]))
        if cursor.rowcount != 1:
            raise RevisionConflict("Job changed")
        return self.get(current["id"])

    def recover_expired(self) -> list[dict]:
        """Safe at startup: leave live leases and waiting/terminal jobs alone."""
        with self.assistant.transaction() as db:
            now = self._now()
            rows = db.execute("SELECT * FROM discovery_jobs WHERE state='running' AND lease_until_ms<=? ORDER BY id",
                              (now,)).fetchall()
            recovered = []
            for row in rows:
                job = _decode(row)
                state = ("outcome_unknown" if job["effect_pending"] else
                         "cancelled" if job["cancel_requested"] else "failed" if _exhausted(job) else "queued")
                recovered.append(self._update(db, job, now, state=state,
                    claim_owner=None, claim_token=None, lease_until_ms=None))
            return recovered

    def claim(self, owner: str, *, lease_ms: int = 30000, kind: str | None = None) -> dict | None:
        _identity(owner)
        integer(lease_ms, "lease_ms", 1)
        if kind is not None:
            _identity(kind)
        with self.assistant.transaction() as db:
            self.recover_expired()
            now = self._now()
            expiry = integer(now + lease_ms, "lease_until_ms")
            query = ("SELECT * FROM discovery_jobs WHERE state='queued' AND available_at_ms<=? "
                     "AND cancel_requested=0 AND effect_pending=0 AND actions_used<25 AND active_ms<300000 "
                     "AND consecutive_no_progress<3")
            params = [now]
            if kind is not None:
                query += " AND kind=?"
                params.append(kind)
            row = db.execute(query + " ORDER BY available_at_ms,created_at_ms,id LIMIT 1", params).fetchone()
            if row is None:
                return None
            current = _decode(row)
            return self._update(db, current, now, state="running", claim_owner=owner,
                claim_token=uuid.uuid4().hex, lease_until_ms=expiry, attempts=current["attempts"] + 1)

    def heartbeat(self, job_id: str, *, expected_revision: int, claim_token: str,
                  lease_ms: int = 30000) -> dict:
        integer(lease_ms, "lease_ms", 1)
        with self.assistant.transaction() as db:
            now = self._now()
            current = self._current(job_id, expected_revision)
            self._claimed(current, claim_token, now)
            expiry = integer(max(current["lease_until_ms"], now + lease_ms), "lease_until_ms")
            return self._update(db, current, now, lease_until_ms=expiry)

    def reserve_usage(self, job_id: str, *, expected_revision: int, claim_token: str,
                      actions: int = 0, active_ms: int = 0, progress: bool | None = None) -> dict:
        """Charge a conservative work/time slice before use, never refund it.

        Report progress=True only after observed progress; False increments the
        no-progress streak. Work must stop when its reserved time slice ends.
        The storage layer cannot measure or enforce a future runtime's clock.
        """
        integer(actions, "actions")
        integer(active_ms, "active_ms")
        if progress is not None and type(progress) is not bool:
            raise ValueError("Invalid progress")
        with self.assistant.transaction() as db:
            now = self._now()
            current = self._current(job_id, expected_revision)
            self._claimed(current, claim_token, now)
            total_actions = current["actions_used"] + actions
            total_time = current["active_ms"] + active_ms
            streak = 0 if progress is True else current["consecutive_no_progress"] + (progress is False)
            if total_actions > 25 or total_time > 300000 or streak > 3 or (
                    _exhausted(current) and (actions or active_ms or progress is True)):
                raise BudgetExhausted("Investigation budget exhausted")
            return self._update(db, current, now, actions_used=total_actions,
                                active_ms=total_time, consecutive_no_progress=streak)

    def mark_effect_pending(self, job_id: str, *, expected_revision: int, claim_token: str) -> dict:
        """Persist uncertainty BEFORE an authorized dispatch, never execute it."""
        with self.assistant.transaction() as db:
            now = self._now()
            current = self._current(job_id, expected_revision)
            self._claimed(current, claim_token, now)
            return self._update(db, current, now, effect_pending=1)

    def transition(self, job_id: str, state: str, *, expected_revision: int,
                   claim_token: str, result: dict | None = None) -> dict:
        """Release a live claim. Pending effects can only become outcome_unknown.

        A future receipt reconciler must resolve that uncertainty explicitly;
        neither a worker success nor a job result completes an obligation.
        """
        if state not in TERMINAL | WAITING | {"queued", "outcome_unknown"}:
            raise InvalidTransition("Invalid running transition")
        if result is not None and not isinstance(result, dict):
            raise ValueError("Result must be an object")
        with self.assistant.transaction() as db:
            now = self._now()
            current = self._current(job_id, expected_revision)
            self._claimed(current, claim_token, now)
            if current["effect_pending"] and state != "outcome_unknown":
                raise InvalidTransition("Possible effect requires outcome reconciliation")
            if state == "queued" and _exhausted(current):
                raise BudgetExhausted("Investigation budget exhausted")
            return self._update(db, current, now, state=state, claim_owner=None, claim_token=None,
                                lease_until_ms=None, result=encode(result) if result is not None else None)

    def resume(self, job_id: str, *, expected_revision: int) -> dict:
        """Resume user/approval waiting; this is scheduling, not approval."""
        with self.assistant.transaction() as db:
            now = self._now()
            current = self._current(job_id, expected_revision)
            if current["state"] not in WAITING:
                raise InvalidTransition("Only waiting jobs can resume")
            if _exhausted(current):
                raise BudgetExhausted("Investigation budget exhausted")
            return self._update(db, current, now, state="queued")

    def cancel(self, job_id: str, *, expected_revision: int) -> dict:
        with self.assistant.transaction() as db:
            now = self._now()
            current = self._current(job_id, expected_revision)
            if current["state"] in TERMINAL:
                return current
            state = "outcome_unknown" if current["effect_pending"] or current["state"] == "outcome_unknown" else "cancelled"
            return self._update(db, current, now, state=state, cancel_requested=1,
                                claim_owner=None, claim_token=None, lease_until_ms=None)

    def reconcile(self, job_id: str, state: str, *, expected_revision: int, result: dict) -> dict:
        """Record explicit investigation outcome; no automatic replay path.

        Trusted future callers must verify receipts/policy separately. This
        local result is not an ActionReceipt and cannot complete an item.
        """
        if state not in TERMINAL or not isinstance(result, dict) or not result:
            raise InvalidTransition("Reconciliation needs a terminal outcome and result")
        with self.assistant.transaction() as db:
            now = self._now()
            current = self._current(job_id, expected_revision)
            if current["state"] != "outcome_unknown":
                raise InvalidTransition("Only uncertain jobs can be reconciled")
            return self._update(db, current, now, state=state, effect_pending=0, result=encode(result))
