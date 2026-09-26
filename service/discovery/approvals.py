"""Durable exact consent, with no effect execution or wire authentication.

The trusted app/service bootstrap creates ONE AppApprovalContext and injects it
into this repository and its authenticated app decision handler. Never expose
that object (or construct a repository) through model tools, source ingestion,
or extension message handlers. Wire dictionaries, including ExactApproval, are
data only. A04 must authenticate app requests before selecting this context.

Consumption is a committed one-use claim, not proof an effect happened. Commit
it BEFORE an executor acts. After a crash, reconcile an uncertain effect; never
release/retry the claim. Future executors must also check live site permission,
private context, document/snapshot freshness and task budgets. Consent cannot
replace those checks. This module does not implement a browser adapter.
"""
from __future__ import annotations

import json
import sqlite3
import time

from service.browser.contracts import require, validate
from service.discovery.contracts import validate_proposal
from service.discovery.store import DiscoveryStore, RevisionConflict, encode, integer
from service.safety import policy


class AppApprovalContext:
    """Opaque in-process capability held only by trusted app-side code.

    Identity, not fields, grants access. Creating a second instance or copying
    this one does not confer the capability installed in an ApprovalStore.
    Python code and direct database access are inside this trust boundary.
    """
    __slots__ = ()

    def __reduce__(self):
        raise TypeError('App approval context cannot be serialized or copied')


class ApprovalStore:
    def __init__(self, assistant, *, app_context: AppApprovalContext, clock=None):
        if type(app_context) is not AppApprovalContext:
            raise TypeError('Trusted app context required')
        self.assistant = assistant
        self.records = DiscoveryStore(assistant)
        self._app_context = app_context
        self._clock = clock if clock is not None else lambda: time.time_ns() // 1_000_000

    def _now(self):
        return integer(self._clock(), 'clock')

    def _authorize(self, context):
        require(context is self._app_context, 'Trusted app context required', 'bridge_unauthorized')

    @staticmethod
    def _policy(intent):
        # Fixed service-side category; neither source text nor callers can label
        # an effect read-only. ALLOW/full-access still never replaces exact consent.
        decision = policy.decide('app_control', intent)
        require(decision.tier != policy.Tier.DENY, decision.reason, 'disabled')

    def get(self, proposal_id: str) -> dict | None:
        """Return durable decision/consumption facts, never a bearer credential."""
        with self.assistant.transaction(write=False) as db:
            row = db.execute("""SELECT d.*, c.consumed_at_ms
                FROM discovery_approval_decisions d LEFT JOIN discovery_approval_consumptions c
                ON d.proposal_id=c.proposal_id WHERE d.proposal_id=?""", (proposal_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            for key in ('intent', 'evidence_ids'):
                result[key] = json.loads(result[key])
            return result

    def decide(self, proposal_id: str, *, app_context: AppApprovalContext,
               decision: str, expected_proposal_revision: int,
               expected_item_storage_revision: int, intent: dict,
               evidence_ids: list[str], expires_at_ms: int) -> dict:
        """Record the app's exact reviewed snapshot once, atomically.

        A rejected/expired decision needs a new proposal and fresh user review;
        no renewal or overwrite is supported. Even rejection compares the
        reviewed snapshot so a delayed response cannot apply to another version.
        """
        self._authorize(app_context)
        require(decision in ('approved', 'rejected'), 'Invalid approval decision')
        integer(expected_proposal_revision, 'proposal revision', 1)
        integer(expected_item_storage_revision, 'item storage revision', 1)
        integer(expires_at_ms, 'expiry')
        intent = validate('ActionIntent', intent)
        with self.assistant.transaction() as db:
            now = self._now()
            require(expires_at_ms > now, 'Approval already expired', 'stale_approval')
            proposal = self.records.get('ActionProposal', proposal_id)
            require(proposal is not None, 'Missing proposal')
            p = proposal['payload']
            item = self.records.get('ActionableItem', p['item_id'])
            require(item is not None, 'Missing obligation')
            if (proposal['revision'] != expected_proposal_revision or
                    item['revision'] != expected_item_storage_revision):
                raise RevisionConflict('Reviewed discovery snapshot changed')
            require(p['state'] == 'proposed' and self.get(proposal_id) is None,
                    'Proposal already decided', 'stale_approval')
            validate_proposal(item['payload'], p)
            require(intent == p['intent'], 'Approval intent mismatch', 'stale_approval')
            require(type(evidence_ids) is list and evidence_ids == p['evidence_ids'] and
                    len(set(evidence_ids)) == len(evidence_ids),
                    'Approval evidence mismatch', 'stale_approval')
            if decision == 'approved':
                self._policy(intent)
            db.execute("""INSERT INTO discovery_approval_decisions VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (proposal_id, decision, proposal['revision'] + 1, p['item_id'], p['item_revision'],
                 item['revision'], encode(intent), encode(evidence_ids), now, expires_at_ms))
            self.records.save('ActionProposal', {**p, 'state': decision},
                              expected_revision=proposal['revision'])
            return self.get(proposal_id)

    def consume(self, proposal_id: str, *, intent: dict, evidence_ids: list[str]) -> dict:
        """Atomically claim durable consent exactly once; does not execute.

        Intent must come from the executor's freshly checked live context, not
        simply be copied from this repository. Invalid attempts leave no claim.
        """
        intent = validate('ActionIntent', intent)
        with self.assistant.transaction() as db:
            now = self._now()
            decision = self.get(proposal_id)
            require(decision is not None and decision['decision'] == 'approved',
                    'Exact app approval required', 'approval_required')
            require(decision['consumed_at_ms'] is None, 'Approval already consumed', 'stale_approval')
            require(decision['decided_at_ms'] <= now < decision['expires_at_ms'],
                    'Approval expired or clock moved backwards', 'stale_approval')
            require(intent == decision['intent'], 'Approval intent mismatch', 'stale_approval')
            require(type(evidence_ids) is list and evidence_ids == decision['evidence_ids'],
                    'Approval evidence mismatch', 'stale_approval')
            proposal = self.records.get('ActionProposal', proposal_id)
            item = self.records.get('ActionableItem', decision['item_id'])
            require(proposal is not None and item is not None and
                    proposal['revision'] == decision['proposal_revision'] and
                    proposal['payload']['state'] == 'approved' and
                    item['revision'] == decision['item_storage_revision'],
                    'Approved discovery snapshot changed', 'stale_approval')
            validate_proposal(item['payload'], proposal['payload'])
            self._policy(intent)
            try:
                db.execute('INSERT INTO discovery_approval_consumptions VALUES (?,?,?,?)',
                           (proposal_id, intent['task_id'], intent['action_id'], now))
            except sqlite3.IntegrityError as exc:
                raise RevisionConflict('Action already claimed; reconcile before retry') from exc
            return self.get(proposal_id)
