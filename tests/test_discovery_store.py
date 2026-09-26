"""A02 persistence regressions, exclusively disposable SQLite state."""
import copy
import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from service.assistant.store import AssistantStore
from service.browser.contracts import ContractViolation
from service.discovery.jobs import JobStore
from service.discovery.approvals import AppApprovalContext, ApprovalStore
from service.safety import policy
from service.discovery.contracts import validate_proposal
from service.discovery.store import DiscoveryStore, RevisionConflict, TABLES

FIXTURE = Path(__file__).resolve().parents[1] / 'test_fixtures/discovery_storage/lifecycle.json'


@pytest.fixture
def records():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    opened = []
    def create():
        assistant = AssistantStore(tmp_path / 'assistant.db')
        opened.append(assistant)
        return DiscoveryStore(assistant)
    yield create
    for store in opened:
        store._db.close()


def seed(store, records):
    store.save('SourceObservation', records['SourceObservation'])
    store.save('Evidence', records['ActionableItem']['evidence'][0])
    store.save('ActionableItem', records['ActionableItem'])


def seed_receipt(store, records):
    store.save('SourceObservation', records['confirmation_observation'])
    store.save('Evidence', records['ActionReceipt']['evidence'][0])
    store.save('ActionReceipt', records['ActionReceipt'])


def seed_approved(store, records):
    proposal = records['ActionProposal']
    pending = {**proposal, 'state': 'proposed'}
    store.save('ActionProposal', pending)
    context = AppApprovalContext()
    approvals = ApprovalStore(store.assistant, app_context=context, clock=lambda: 1790388000000)
    approvals.decide(proposal['id'], app_context=context, decision='approved',
        expected_proposal_revision=1, expected_item_storage_revision=1,
        intent=proposal['intent'], evidence_ids=proposal['evidence_ids'], expires_at_ms=1790388010000)
    approvals.consume(proposal['id'], intent=proposal['intent'], evidence_ids=proposal['evidence_ids'])


def test_records_roundtrip_remain_distinct_after_restart(stores, records):
    store = stores()
    seed(store, records)
    seed_receipt(store, records)
    seed_approved(store, records)
    for kind in ('BrowserTask', 'ScheduledBlock', 'ExternalRecord'):
        store.save(kind, records[kind])
    reopened = stores()
    for kind in TABLES:
        value = records['ActionableItem']['evidence'][0] if kind == 'Evidence' else records[kind]
        assert reopened.get(kind, value['id'])['payload'] == value
    assert reopened.get('ActionableItem', records['ActionableItem']['id'])['payload']['state'] == 'tracked'
    assert reopened.get('ActionableItem', records['ScheduledBlock']['id']) is None
    with pytest.raises(ContractViolation):
        reopened.save('ActionableItem', records['ScheduledBlock'])


def test_captures_are_immutable_and_exact_retries_are_idempotent(stores, records):
    store = stores()
    seed(store, records)
    seed_receipt(store, records)
    for kind, field in [('SourceObservation', 'observed_at_ms'), ('Evidence', 'captured_at_ms'),
                        ('ActionReceipt', 'recorded_at_ms')]:
        value = records['ActionableItem']['evidence'][0] if kind == 'Evidence' else records[kind]
        original = store.get(kind, value['id'])
        assert store.save(kind, value) == original
        with pytest.raises(RevisionConflict):
            store.save(kind, {**value, field: value[field] + 1}, expected_revision=1)
        assert stores().get(kind, value['id']) == original


@pytest.mark.parametrize('field,value', [('observation_id','missing'),('source_revision','stale'),
    ('captured_at_ms', 0), ('quote','invented')])
def test_evidence_requires_matching_capture(stores, records, field, value):
    store = stores()
    store.save('SourceObservation', records['SourceObservation'])
    with pytest.raises(ContractViolation):
        store.save('Evidence', {**records['ActionableItem']['evidence'][0], field: value})
    assert store.get('Evidence', 'e.assignment') is None


def test_content_revisions_overrides_history_and_stale_cas(stores, records):
    store = stores()
    seed(store, records)
    item = records['ActionableItem']
    override = {'due_at_ms': 1900000000000, 'reason': 'manual correction'}
    store.set_overrides(item['id'], override, expected_revision=1)
    changed = {**item, 'title': 'Revised', 'revision': 2, 'supersedes_revision': 1}
    with pytest.raises(RevisionConflict):
        store.save('ActionableItem', changed, expected_revision=1)
    updated = store.save('ActionableItem', changed, expected_revision=2)
    assert updated['overrides'] == override
    assert updated['revision'] == 3
    assert store.get('ActionableItem', item['id'], revision=1)['payload'] == item
    assert store.get('ActionableItem', item['id'], revision=2)['overrides'] == override
    with pytest.raises(ContractViolation):
        store.save('ActionableItem', {**changed, 'title': 'Changed without revision'}, expected_revision=3)
    assert stores().get('SourceObservation', 'obs.assignment')['payload'] == records['SourceObservation']
    with pytest.raises(RevisionConflict):
        store.set_overrides(item['id'], {}, expected_revision=2)


def test_cas_is_atomic_across_connections(stores, records):
    a, b = stores(), stores()
    seed(a, records)
    barrier = threading.Barrier(2)
    def edit(store):
        barrier.wait(timeout=5)
        try:
            return store.set_overrides('obligation.42', {'manual': True}, expected_revision=1)
        except RevisionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, [a, b]))
    assert sum(r is not None for r in results) == 1


def test_completion_requires_linked_receipt_and_never_follows_job_success(stores, records):
    store = stores()
    seed(store, records)
    seed_approved(store, records)
    item = {**records['ActionableItem'], 'state':'completed', 'completion_receipt_id':'receipt.1'}
    with pytest.raises(ContractViolation):
        store.save('ActionableItem', item, expected_revision=1)
    seed_receipt(store, records)
    assert store.get('ActionableItem', item['id'])['payload']['state'] == 'tracked'
    done = store.save('ActionableItem', item, expected_revision=1)
    assert done['payload']['state'] == 'completed'
    assert done['revision'] == 2


@pytest.mark.parametrize('field,value', [('completes_obligation',False),('task_id','wrong'),
    ('action_id','wrong'),('proposal_id',None)])
def test_receipt_mismatch_cannot_complete(stores, records, field, value):
    store = stores()
    seed(store, records)
    seed_approved(store, records)
    records['ActionReceipt'][field] = value
    seed_receipt(store, records)
    with pytest.raises(ContractViolation):
        store.save('ActionableItem', {**records['ActionableItem'], 'state':'completed',
            'completion_receipt_id':'receipt.1'}, expected_revision=1)


def test_uncertain_receipt_never_completes(stores, records):
    store = stores()
    seed(store, records)
    seed_approved(store, records)
    receipt = {**records['ActionReceipt'], 'status':'uncertain', 'completes_obligation':False}
    records['ActionReceipt'] = receipt
    seed_receipt(store, records)
    with pytest.raises(ContractViolation):
        store.save('ActionableItem', {**records['ActionableItem'], 'state':'completed',
            'completion_receipt_id':'receipt.1'}, expected_revision=1)


def test_stale_or_changed_proposal_rejected(stores, records):
    store = stores()
    seed(store, records)
    proposal = records['ActionProposal']
    seed_approved(store, records)
    changed = copy.deepcopy(proposal)
    changed['intent']['target_id'] = 'another.target'
    with pytest.raises(ContractViolation):
        store.save('ActionProposal', changed, expected_revision=2)
    with pytest.raises(ContractViolation):
        store.save('ActionProposal', {**proposal, 'id':'new', 'item_revision':2})


def test_browser_budget_cannot_decrease(stores, records):
    store = stores()
    task = {**records['BrowserTask'], 'actions_used':3, 'active_ms':1500}
    store.save('BrowserTask', task)
    for field in ('actions_used','active_ms'):
        with pytest.raises(ContractViolation):
            store.save('BrowserTask', {**task, field:0}, expected_revision=1)


def test_composed_transaction_and_nested_failure_roll_back(stores, records):
    store = stores()
    jobs = JobStore(store.assistant, clock=lambda: 1000)
    with pytest.raises(RuntimeError):
        with store.assistant.transaction():
            seed(store, records)
            jobs.enqueue('extract', {}, job_id='job.1')
            raise RuntimeError('injected crash')
    assert stores().get('SourceObservation', 'obs.assignment') is None
    assert jobs.get('job.1') is None
    with store.assistant.transaction():
        store.save('SourceObservation', records['SourceObservation'])
        with pytest.raises(ContractViolation):
            store.save('Evidence', {**records['ActionableItem']['evidence'][0], 'quote':'missing'})
        store.save('Evidence', records['ActionableItem']['evidence'][0])
    assert stores().get('Evidence', 'e.assignment') is not None


def test_additive_migration_preserves_legacy_and_today_rows(tmp_path, monkeypatch):
    from service.discovery import store as module
    path = tmp_path / 'legacy.db'
    real_migrate = module.migrate
    monkeypatch.setattr(module, 'migrate', lambda db: None)
    old = AssistantStore(path)
    manual = old.add_manual('Manual override', 1900000000)
    old.set_status(manual['id'], 'dismissed')
    old.mark_notified(manual['id'], 'due')
    old._db.execute('INSERT INTO today_tasks VALUES (?,?,?,?,?)',
                    ('today','2026-09-25', '{"pinned_start":1900000000,"title":"Keep"}',7,123.5))
    old._db.execute('INSERT INTO today_preferences VALUES (?,?,?,?)',
                    ('2026-09-25','UTC','{"start_minute":600}',4))
    old._db.execute('INSERT INTO today_source_sync VALUES (?,?)',
                    ('calendar','{"snapshot_started_at":123,"last_sync":124}'))
    old._db.commit()
    tables = [r[0] for r in old._db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {t: old._db.execute(f'SELECT * FROM {t}').fetchall() for t in tables}
    old._db.close()
    monkeypatch.setattr(module, 'migrate', real_migrate)
    for _ in range(3):
        reopened = AssistantStore(path)
        try:
            assert {t: reopened._db.execute(f'SELECT * FROM {t}').fetchall() for t in tables} == before
            assert reopened._db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert len(reopened._db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_discovery_%'").fetchall()) == 5
        finally:
            reopened._db.close()


@pytest.mark.parametrize('failure_point', ['CREATE INDEX IF NOT EXISTS idx_discovery_job_lease',
    'CREATE TABLE IF NOT EXISTS discovery_approval_consumptions',
    'CREATE TRIGGER IF NOT EXISTS discovery_record_history_no_delete'])
def test_failure_during_discovery_ddl_rolls_back_entire_migration(tmp_path, monkeypatch, failure_point):
    from service.assistant import store as module
    path = tmp_path / 'failure.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sentinel(value TEXT)')
        db.execute("INSERT INTO sentinel VALUES ('preserve')")
        before = list(db.iterdump())
    connect = sqlite3.connect
    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if failure_point in sql:
                raise sqlite3.OperationalError('injected DDL failure')
            return result
    with monkeypatch.context() as patch:
        patch.setattr(module.sqlite3, 'connect', lambda *a, **kw: connect(*a, **kw, factory=FailingConnection))
        with pytest.raises(sqlite3.OperationalError, match='injected'):
            AssistantStore(path)
    with connect(path) as db:
        assert list(db.iterdump()) == before
    AssistantStore(path)._db.close()


def test_read_transaction_cannot_be_upgraded(stores, records):
    store = stores()
    with store.assistant.transaction(write=False):
        assert store.get('SourceObservation', 'obs.assignment') is None
        with pytest.raises(RuntimeError, match='Cannot upgrade'):
            store.save('SourceObservation', records['SourceObservation'])
    store.save('SourceObservation', records['SourceObservation'])
    assert stores().get('SourceObservation', 'obs.assignment') is not None


@pytest.mark.parametrize('state', ['queued', 'running', 'waiting_user', 'handed_off'])
def test_exhausted_browser_progress_cannot_reset_after_restart(stores, records, state):
    store = stores()
    task = {**records['BrowserTask'], 'state':'waiting_user', 'consecutive_no_progress':3}
    store.save('BrowserTask', task)
    with pytest.raises(ContractViolation):
        stores().save('BrowserTask', {**task, 'state':state, 'consecutive_no_progress':0}, expected_revision=1)
    assert stores().get('BrowserTask', task['id'])['payload'] == task
    assert store.get('BrowserTask', task['id'], revision=2) is None


@pytest.mark.parametrize('change', ['revision', 'dismissed', 'completed'])
@pytest.mark.parametrize('retired', ['expired', 'rejected'])
def test_stale_proposal_can_retire_but_never_reactivate(stores, records, change, retired):
    store = stores()
    seed(store, records)
    proposal = records['ActionProposal']
    seed_approved(store, records)
    item = records['ActionableItem']
    if change == 'revision':
        item = {**item, 'title':'Updated', 'revision':2, 'supersedes_revision':1}
    elif change == 'completed':
        seed_receipt(store, records)
        item = {**item, 'state':'completed', 'completion_receipt_id':'receipt.1'}
    else:
        item = {**item, 'state':'dismissed'}
    store.save('ActionableItem', item, expected_revision=1)
    retirement = store.save('ActionProposal', {**proposal, 'state':retired}, expected_revision=2)
    assert stores().get('ActionProposal', proposal['id']) == retirement
    for state in ('proposed','approved'):
        with pytest.raises(ContractViolation):
            store.save('ActionProposal', {**proposal, 'state':state}, expected_revision=3)
    changed = copy.deepcopy(proposal)
    changed['state'] = retired
    changed['intent']['target_id'] = 'different'
    with pytest.raises(ContractViolation):
        store.save('ActionProposal', changed, expected_revision=3)
    assert store.get('ActionProposal', proposal['id'], revision=4) is None
    assert store.get('ActionProposal', proposal['id']) == retirement


def test_job_success_is_not_obligation_completion(stores, records):
    store = stores()
    seed(store, records)
    jobs = JobStore(store.assistant, clock=lambda: 1000)
    jobs.enqueue('extract', {'item_id':records['ActionableItem']['id']}, job_id='job')
    job = jobs.claim('worker')
    jobs.transition('job', 'succeeded', expected_revision=job['revision'], claim_token=job['claim_token'])
    assert store.get('ActionableItem', records['ActionableItem']['id'])['payload'] == records['ActionableItem']


@pytest.mark.parametrize('terminal', ['completed', 'dismissed'])
@pytest.mark.parametrize('reopened_state', ['tracked', 'candidate'])
def test_terminal_reactivation_invalidates_old_approval_and_receipt_after_restart(stores, records, terminal, reopened_state):
    store = stores()
    seed(store, records)
    proposal = records['ActionProposal']
    seed_approved(store, records)
    seed_receipt(store, records)
    item = records['ActionableItem']
    closed = {**item, 'state':terminal,
              'completion_receipt_id':'receipt.1' if terminal == 'completed' else None}
    store.save('ActionableItem', closed, expected_revision=1)
    store = stores()
    with pytest.raises(ContractViolation, match='terminal item needs a new revision'):
        store.save('ActionableItem', {**item, 'state':reopened_state}, expected_revision=2)
    assert stores().get('ActionableItem', item['id'])['payload'] == closed
    assert store.get('ActionableItem', item['id'], revision=3) is None

    reopened = {**item, 'revision':2, 'supersedes_revision':1}
    store.save('ActionableItem', reopened, expected_revision=2)
    store = stores()
    current = store.get('ActionableItem', item['id'])
    old_proposal = store.get('ActionProposal', proposal['id'])
    assert current['payload']['revision'] == 2
    with pytest.raises(ContractViolation, match='Stale proposal'):
        validate_proposal(current['payload'], old_proposal['payload'])
    with pytest.raises(ContractViolation):
        store.save('ActionProposal', proposal, expected_revision=old_proposal['revision'])
    with pytest.raises(ContractViolation, match='immutable'):
        store.save('ActionProposal', {**proposal, 'item_revision':2}, expected_revision=old_proposal['revision'])
    with pytest.raises(ContractViolation, match='Receipt item mismatch'):
        store.save('ActionableItem', {**reopened, 'state':'completed', 'completion_receipt_id':'receipt.1'}, expected_revision=3)
    assert stores().get('ActionableItem', item['id']) == current
    assert store.get('ActionableItem', item['id'], revision=4) is None

    # A new revision-bound proposal can be offered, with a fresh exact action.
    fresh = copy.deepcopy(proposal)
    fresh.update(id='proposal.2', item_revision=2, state='proposed')
    fresh['intent']['action_id'] = 'action.2'
    assert store.save('ActionProposal', fresh)['payload'] == fresh
    assert stores().get('ActionProposal', 'proposal.2')['payload']['state'] == 'proposed'
