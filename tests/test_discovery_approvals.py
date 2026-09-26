"""A03 consent tests: synthetic captures and disposable SQLite only, no effects."""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from service.assistant.store import AssistantStore
from service.browser.contracts import ContractViolation
from service.discovery.approvals import AppApprovalContext, ApprovalStore
from service.discovery.store import DiscoveryStore, RevisionConflict
from service.safety import policy


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, '_READ_ONLY', False)
    monkeypatch.setattr(policy, '_FULL_ACCESS', False)
    records = json.loads((Path(__file__).resolve().parents[1] /
        'test_fixtures/discovery_storage/lifecycle.json').read_text())
    records['ActionProposal']['state'] = 'proposed'
    now = [1790388000000]
    context = AppApprovalContext()
    opened = []

    def connect():
        assistant = AssistantStore(tmp_path / 'synthetic.db')
        opened.append(assistant)
        return ApprovalStore(assistant, app_context=context, clock=lambda: now[0])

    approvals = connect()
    store = approvals.records
    store.save('SourceObservation', records['SourceObservation'])
    store.save('Evidence', records['ActionableItem']['evidence'][0])
    store.save('ActionableItem', records['ActionableItem'])
    store.save('ActionProposal', records['ActionProposal'])
    yield approvals, store, records, context, now, connect
    for assistant in opened:
        assistant._db.close()


def decide(env, **changes):
    approvals, _, records, context, now, _ = env
    p = records['ActionProposal']
    kwargs = dict(app_context=context, decision='approved', expected_proposal_revision=1,
        expected_item_storage_revision=1, intent=p['intent'], evidence_ids=p['evidence_ids'],
        expires_at_ms=now[0] + 1000)
    kwargs.update(changes)
    return approvals.decide(p['id'], **kwargs)


def consume(env, **changes):
    approvals, _, records, _, _, _ = env
    p = records['ActionProposal']
    kwargs = dict(intent=p['intent'], evidence_ids=p['evidence_ids'])
    kwargs.update(changes)
    return approvals.consume(p['id'], **kwargs)


def receipt(env):
    _, store, records, _, _, _ = env
    store.save('SourceObservation', records['confirmation_observation'])
    store.save('Evidence', records['ActionReceipt']['evidence'][0])
    store.save('ActionReceipt', records['ActionReceipt'])
    return {**records['ActionableItem'], 'state': 'completed', 'completion_receipt_id': 'receipt.1'}


@pytest.mark.parametrize('fake', [None, 'app', {'authority': 'app'}, AppApprovalContext()])
def test_forged_authority_leaves_no_decision_or_history(env, fake):
    approvals, store, records, _, _, _ = env
    with pytest.raises(ContractViolation, match='Trusted app context'):
        decide(env, app_context=fake)
    assert approvals.get('proposal.1') is None
    assert store.get('ActionProposal', 'proposal.1')['payload'] == records['ActionProposal']
    assert store.get('ActionProposal', 'proposal.1', revision=2) is None


def test_self_asserted_wire_state_and_approval_are_not_authority(env):
    approvals, store, records, _, _, _ = env
    p = records['ActionProposal']
    for value, revision in [({**p, 'state': 'approved'}, 1),
                            ({**p, 'id': 'forged', 'state': 'approved'}, 0)]:
        with pytest.raises(ContractViolation, match='trusted app decision'):
            store.save('ActionProposal', value, expected_revision=revision)
    with pytest.raises(ContractViolation, match='Exact app approval required'):
        consume(env)
    with pytest.raises(TypeError):
        ApprovalStore(store.assistant, app_context={'authority': 'app'})
    with pytest.raises(TypeError):
        copy.copy(env[3])
    assert approvals.get('proposal.1') is None


@pytest.mark.parametrize('phase', ['decision', 'consume'])
@pytest.mark.parametrize('field,value', [('action_id', 'other'), ('task_id', 'other'),
    ('snapshot_id', 'navigated'), ('target_id', 'other'), ('consequential', False)])
def test_every_intent_field_is_exact(env, phase, field, value):
    if phase == 'consume':
        decide(env)
    intent = {**env[2]['ActionProposal']['intent'], field: value}
    with pytest.raises(ContractViolation, match='intent mismatch'):
        (decide if phase == 'decision' else consume)(env, intent=intent)
    result = env[0].get('proposal.1')
    assert result is None or result['consumed_at_ms'] is None


@pytest.mark.parametrize('field,value', [('text', 'different private text'), ('private_data', False),
    ('command', 'select'), ('url', 'https://other.example.invalid/')])
def test_typing_and_navigation_intents_are_bound(env, field, value):
    _, store, records, _, _, _ = env
    p = records['ActionProposal']
    intent = {**p['intent'], 'command': 'fill', 'text': 'synthetic text', 'private_data': True}
    if field == 'url':
        intent.update(command='navigate', text=None, target_id=None, private_data=False,
                      url='https://school.example.invalid/')
    elif field == 'command':
        intent['private_data'] = False  # Both fill and select must be valid intents.
    fresh = {**p, 'id': 'proposal.typed', 'intent': intent}
    store.save('ActionProposal', fresh)
    records['ActionProposal'] = fresh
    decide(env)
    with pytest.raises(ContractViolation, match='intent mismatch'):
        consume(env, intent={**intent, field: value})


@pytest.mark.parametrize('evidence', [[], ['e.confirmation'], ['e.assignment', 'e.assignment'],
    ['e.assignment', 'e.other'], 'e.assignment'])
@pytest.mark.parametrize('phase', ['decision', 'consume'])
def test_evidence_ids_match_exactly(env, phase, evidence):
    if phase == 'consume':
        decide(env)
    with pytest.raises(ContractViolation, match='evidence mismatch'):
        (decide if phase == 'decision' else consume)(env, evidence_ids=evidence)


@pytest.mark.parametrize('change', ['content', 'override', 'dismiss', 'retire'])
@pytest.mark.parametrize('phase', ['decision', 'consume'])
def test_stale_item_and_proposal_snapshots_fail_closed(env, change, phase):
    _, store, records, _, _, _ = env
    if phase == 'consume':
        decide(env)
    p, item = records['ActionProposal'], records['ActionableItem']
    if change == 'override':
        store.set_overrides(item['id'], {'title': 'manual edit'}, expected_revision=1)
    elif change == 'content':
        store.save('ActionableItem', {**item, 'title': 'Edited', 'revision': 2,
            'supersedes_revision': 1}, expected_revision=1)
    elif change == 'dismiss':
        store.save('ActionableItem', {**item, 'state': 'dismissed'}, expected_revision=1)
    else:
        store.save('ActionProposal', {**p, 'state': 'expired'}, expected_revision=2 if phase == 'consume' else 1)
    with pytest.raises((RevisionConflict, ContractViolation)):
        (decide if phase == 'decision' else consume)(env)


def test_rejection_is_durable_and_cannot_be_overwritten(env):
    approvals, store, _, _, _, connect = env
    rejected = decide(env, decision='rejected')
    assert connect().get('proposal.1') == rejected
    assert store.get('ActionProposal', 'proposal.1')['payload']['state'] == 'rejected'
    with pytest.raises(ContractViolation, match='already decided'):
        decide(env, expected_proposal_revision=2)
    with pytest.raises(ContractViolation, match='approval required'):
        consume(env)
    assert approvals.get('proposal.1') == rejected


@pytest.mark.parametrize('offset', [-1, 1000, 1001])
def test_expiry_boundary_and_backward_clock(env, offset):
    decide(env)
    env[4][0] += offset
    with pytest.raises(ContractViolation, match='expired or clock'):
        consume(env)
    assert env[5]().get('proposal.1')['consumed_at_ms'] is None


@pytest.mark.parametrize('expiry', [0, True, -1, 1.5, 9007199254740992])
def test_invalid_expiry_never_decides(env, expiry):
    with pytest.raises(ValueError):
        decide(env, expires_at_ms=expiry)
    assert env[0].get('proposal.1') is None


def test_fresh_claim_survives_restart_and_replay_fails(env):
    decided = decide(env)
    env[4][0] += 999
    claimed = consume(env)
    assert claimed['consumed_at_ms'] == env[4][0]
    reopened = env[5]()
    assert reopened.get('proposal.1') == claimed
    with pytest.raises(ContractViolation, match='already consumed'):
        reopened.consume('proposal.1', intent=decided['intent'], evidence_ids=decided['evidence_ids'])
    assert env[1].get('ActionableItem', 'obligation.42')['payload']['state'] == 'tracked'


@pytest.mark.parametrize('operation', ['decide', 'consume'])
def test_concurrent_connections_allow_one_winner(env, operation):
    if operation == 'consume':
        decide(env)
    a, b = env[0], env[5]()
    barrier = threading.Barrier(2)

    def run(approvals):
        local = (approvals, *env[1:])
        barrier.wait(timeout=5)
        try:
            return (decide if operation == 'decide' else consume)(local)
        except (ContractViolation, RevisionConflict):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [a, b]))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize('operation', ['decide', 'consume'])
def test_crash_rolls_back_composed_transaction(env, operation):
    if operation == 'consume':
        decide(env)
    before = env[0].get('proposal.1')
    proposal = env[1].get('ActionProposal', 'proposal.1')
    with pytest.raises(KeyboardInterrupt):
        with env[0].assistant.transaction():
            (decide if operation == 'decide' else consume)(env)
            raise KeyboardInterrupt('crash before commit')
    reopened = env[5]()
    assert reopened.get('proposal.1') == before
    assert reopened.records.get('ActionProposal', 'proposal.1') == proposal
    (decide if operation == 'decide' else consume)(env)


@pytest.mark.parametrize('committed', [False, True])
def test_process_exit_preserves_only_committed_claim(env, committed):
    decide(env)
    # Exit without Python cleanup: SQLite must roll back an open transaction,
    # while a committed claim must still reject replay after process loss.
    program = """
import os, sys
from pathlib import Path
from service.assistant.store import AssistantStore
from service.discovery.approvals import ApprovalStore, AppApprovalContext
from service.safety import policy
policy.set_read_only(False)
policy.set_full_access(False)
a = ApprovalStore(AssistantStore(Path(sys.argv[1])), app_context=AppApprovalContext(), clock=lambda: 1790388000000)
d = a.get('proposal.1')
if sys.argv[2] == 'False':
    with a.assistant.transaction():
        a.consume('proposal.1', intent=d['intent'], evidence_ids=d['evidence_ids'])
        os._exit(73)
a.consume('proposal.1', intent=d['intent'], evidence_ids=d['evidence_ids'])
os._exit(73)
"""
    result = subprocess.run([sys.executable, '-c', program, str(env[0].assistant.path), str(committed)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 73, result.stderr
    reopened = env[5]()
    assert (reopened.get('proposal.1')['consumed_at_ms'] is not None) == committed
    if committed:
        with pytest.raises(ContractViolation, match='already consumed'):
            consume(env)
    else:
        consume(env)


def test_failure_between_decision_and_proposal_rolls_back(env, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(env[0].records, 'save', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('crash')))
        with pytest.raises(RuntimeError, match='crash'):
            decide(env)
    assert env[5]().get('proposal.1') is None
    assert env[1].get('ActionProposal', 'proposal.1', revision=2) is None
    decide(env)


@pytest.mark.parametrize('phase', ['decision', 'consume'])
def test_existing_safety_deny_is_not_overridden(env, monkeypatch, phase):
    if phase == 'consume':
        decide(env)
    monkeypatch.setattr(policy, '_READ_ONLY', True)
    with pytest.raises(ContractViolation, match='view-only'):
        (decide if phase == 'decision' else consume)(env)


def test_full_access_does_not_replace_exact_approval(env, monkeypatch):
    monkeypatch.setattr(policy, '_FULL_ACCESS', True)
    with pytest.raises(ContractViolation, match='approval required'):
        consume(env)
    decide(env)
    consume(env)


def test_another_proposal_cannot_replay_same_action(env):
    decide(env)
    consume(env)
    approvals, store, records, context, now, _ = env
    p = {**records['ActionProposal'], 'id': 'proposal.2'}
    store.save('ActionProposal', p)
    approvals.decide(p['id'], app_context=context, decision='approved',
        expected_proposal_revision=1, expected_item_storage_revision=1,
        intent=p['intent'], evidence_ids=p['evidence_ids'], expires_at_ms=now[0] + 1000)
    with pytest.raises(RevisionConflict, match='already claimed'):
        approvals.consume(p['id'], intent=p['intent'], evidence_ids=p['evidence_ids'])
    assert approvals.get(p['id'])['consumed_at_ms'] is None


@pytest.mark.parametrize('retired', ['expired', 'rejected'])
@pytest.mark.parametrize('retire_first', [True, False])
def test_historical_completion_proof_survives_retirement(env, retired, retire_first):
    decide(env)
    consume(env)
    _, store, records, _, _, connect = env
    completed = receipt(env)
    if not retire_first:
        store.save('ActionableItem', completed, expected_revision=1)
    store.save('ActionProposal', {**records['ActionProposal'], 'state': retired}, expected_revision=2)
    reopened = connect().records
    approved = reopened.completion_proposal('receipt.1')
    assert approved['revision'] == 2 and approved['payload']['state'] == 'approved'
    assert reopened.get('ActionProposal', 'proposal.1')['payload']['state'] == retired
    done = reopened.save('ActionableItem', completed, expected_revision=1 if retire_first else 2)
    assert done['payload'] == completed
    # Reopening is a new obligation revision and cannot reuse historical proof.
    reopened_item = {**records['ActionableItem'], 'revision': 2, 'supersedes_revision': 1}
    current = reopened.save('ActionableItem', reopened_item, expected_revision=done['revision'])
    with pytest.raises(ContractViolation, match='Receipt item mismatch'):
        reopened.save('ActionableItem', {**completed, 'revision': 2, 'supersedes_revision': 1},
                      expected_revision=current['revision'])


def test_unconsumed_approval_cannot_complete(env):
    decide(env)
    completed = receipt(env)
    with pytest.raises(ContractViolation, match='consumed app approval'):
        env[1].save('ActionableItem', completed, expected_revision=1)


def test_receipt_before_consumption_cannot_complete(env):
    decide(env, expires_at_ms=env[4][0] + 10000)
    env[4][0] += 2000
    consume(env)
    completed = receipt(env)
    with pytest.raises(ContractViolation, match='predates'):
        env[1].save('ActionableItem', completed, expected_revision=1)


def test_history_and_authority_facts_cannot_be_rewritten(env):
    decide(env)
    consume(env)
    for table in ('discovery_approval_decisions', 'discovery_approval_consumptions', 'discovery_record_history'):
        for sql in (f'DELETE FROM {table}', f'UPDATE {table} SET rowid=rowid'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                with env[0].assistant.transaction() as db:
                    db.execute(sql)
    assert env[5]().get('proposal.1')['consumed_at_ms'] is not None


def test_migration_does_not_trust_legacy_approved_state(tmp_path):
    # An A02 row can say approved, but no authenticated A03 decision exists.
    records = json.loads((Path(__file__).resolve().parents[1] /
        'test_fixtures/discovery_storage/lifecycle.json').read_text())
    path = tmp_path / 'legacy.db'
    assistant = AssistantStore(path)
    with assistant.transaction() as db:
        db.execute('INSERT INTO discovery_proposals VALUES (?,1,?,?)',
                   ('proposal.1', json.dumps(records['ActionProposal']), '{}'))
    assistant._db.close()
    assistant = AssistantStore(path)
    try:
        approvals = ApprovalStore(assistant, app_context=AppApprovalContext())
        assert approvals.get('proposal.1') is None
        assert DiscoveryStore(assistant).get('ActionProposal', 'proposal.1')['payload']['state'] == 'approved'
        with pytest.raises(ContractViolation, match='approval required'):
            approvals.consume('proposal.1', intent=records['ActionProposal']['intent'], evidence_ids=['e.assignment'])
    finally:
        assistant._db.close()
