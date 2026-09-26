"""Durable job races and crash recovery using isolated SQLite connections."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from service.assistant.store import AssistantStore
from service.discovery.jobs import JobStore, ClaimConflict, InvalidTransition, BudgetExhausted
from service.discovery.store import RevisionConflict


@pytest.fixture
def clock():
    return [1000]


@pytest.fixture
def stores(tmp_path, clock):
    opened = []
    def create():
        assistant = AssistantStore(tmp_path / 'assistant.db')
        opened.append(assistant)
        return JobStore(assistant, clock=lambda: clock[0])
    yield create
    for assistant in opened:
        assistant._db.close()


def credentials(job):
    return dict(expected_revision=job['revision'], claim_token=job['claim_token'])


def running(store, job_id='job'):
    store.enqueue('extract', {'source':'synthetic'}, job_id=job_id)
    return store.claim('worker', lease_ms=100)


def test_enqueue_idempotency_order_and_kind_filter(stores, clock):
    store = stores()
    later = store.enqueue('extract', {}, job_id='later', available_at_ms=2000)
    first = store.enqueue('extract', {}, job_id='first')
    clock[0] += 1
    assert store.enqueue('extract', {}, job_id='first') == first
    with pytest.raises(RevisionConflict):
        store.enqueue('extract', {'changed':True}, job_id='first')
    with pytest.raises(RevisionConflict):
        store.enqueue('extract', {}, job_id='later', available_at_ms=2001)
    assert store.claim('worker', kind='other') is None
    assert store.claim('worker')['id'] == first['id']
    assert store.claim('second') is None
    clock[0] = 2000
    assert store.claim('second')['id'] == later['id']


def test_one_claim_across_simultaneous_connections(stores):
    clients = [stores() for _ in range(8)]
    clients[0].enqueue('extract', {}, job_id='single')
    barrier = threading.Barrier(len(clients))
    def claim(pair):
        index, store = pair
        barrier.wait(timeout=10)
        return store.claim(f'worker.{index}')
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        claimed = list(pool.map(claim, enumerate(clients)))
    assert sum(job is not None for job in claimed) == 1
    assert clients[0].get('single')['attempts'] == 1


def test_expiry_restart_and_same_owner_aba_fence(stores, clock):
    store = stores()
    old = running(store)
    clock[0] = old['lease_until_ms'] - 1
    assert stores().recover_expired() == []
    clock[0] += 1
    with pytest.raises(ClaimConflict):
        store.transition('job', 'succeeded', **credentials(old))
    newer = stores().claim('worker', lease_ms=100)
    assert newer['claim_token'] != old['claim_token']
    assert newer['attempts'] == 2
    with pytest.raises(RevisionConflict):
        store.heartbeat('job', **credentials(old))
    with pytest.raises(ClaimConflict):
        store.transition('job', 'succeeded', expected_revision=newer['revision'], claim_token=old['claim_token'])
    assert stores().get('job') == newer


def test_heartbeat_revision_cas_and_lease_does_not_shrink(stores, clock):
    store = stores()
    job = running(store)
    clock[0] += 10
    renewed = store.heartbeat('job', **credentials(job), lease_ms=200)
    assert renewed['lease_until_ms'] == clock[0] + 200
    with pytest.raises(RevisionConflict):
        store.cancel('job', expected_revision=job['revision'])
    shorter = store.heartbeat('job', **credentials(renewed), lease_ms=1)
    assert shorter['lease_until_ms'] == renewed['lease_until_ms']


@pytest.mark.parametrize('state', ['waiting_approval','waiting_user','succeeded','failed','cancelled','outcome_unknown'])
def test_waiting_and_terminal_states_survive_restart(stores, clock, state):
    store = stores()
    job = running(store)
    saved = store.transition('job', state, **credentials(job), result={'reason':'synthetic'})
    clock[0] += 1_000_000
    assert stores().recover_expired() == []
    assert stores().get('job') == saved
    assert store.claim('next') is None
    if state in ('waiting_user','waiting_approval'):
        resumed = store.resume('job', expected_revision=saved['revision'])
        assert resumed['state'] == 'queued'
        assert store.claim('next')['state'] == 'running'
    else:
        with pytest.raises(InvalidTransition):
            store.resume('job', expected_revision=saved['revision'])


def test_pending_effect_expiry_never_retries_and_reconciliation_is_explicit(stores, clock):
    store = stores()
    job = running(store)
    pending = store.mark_effect_pending('job', **credentials(job))
    with pytest.raises(InvalidTransition):
        store.transition('job', 'succeeded', **credentials(pending))
    clock[0] = pending['lease_until_ms']
    recovered = stores().recover_expired()[0]
    assert recovered['state'] == 'outcome_unknown'
    assert recovered['effect_pending']
    assert store.claim('next') is None
    with pytest.raises(InvalidTransition):
        store.reconcile('job', 'queued', expected_revision=recovered['revision'], result={'receipt':'x'})
    with pytest.raises(InvalidTransition):
        store.reconcile('job', 'succeeded', expected_revision=recovered['revision'], result={})
    result = store.reconcile('job', 'succeeded', expected_revision=recovered['revision'],
                             result={'receipt_id':'synthetic.verified'})
    assert result['state'] == 'succeeded' and not result['effect_pending']
    assert store.claim('next') is None


@pytest.mark.parametrize('pending', [False, True])
def test_cancel_fences_running_worker_and_preserves_uncertainty(stores, pending):
    store = stores()
    job = running(store)
    if pending:
        job = store.mark_effect_pending('job', **credentials(job))
    cancelled = stores().cancel('job', expected_revision=job['revision'])
    assert cancelled['state'] == ('outcome_unknown' if pending else 'cancelled')
    assert cancelled['cancel_requested']
    assert cancelled['claim_token'] is None
    with pytest.raises(RevisionConflict):
        store.transition('job', 'succeeded', **credentials(job))
    assert store.claim('next') is None


def test_cancel_vs_complete_has_one_cas_winner(stores):
    a, b = stores(), stores()
    job = running(a)
    barrier = threading.Barrier(2)
    def finish(cancel):
        barrier.wait(timeout=5)
        try:
            return (b.cancel('job', expected_revision=job['revision']) if cancel else
                    a.transition('job', 'succeeded', **credentials(job)))
        except RevisionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, [False, True]))
    assert sum(r is not None for r in results) == 1
    assert a.get('job')['state'] in ('succeeded','cancelled')


def test_cancel_queued_waiting_and_unknown(stores):
    store = stores()
    queued = store.enqueue('extract', {})
    assert store.cancel(queued['id'], expected_revision=queued['revision'])['state'] == 'cancelled'
    job = running(store)
    waiting = store.transition('job', 'waiting_user', **credentials(job))
    assert store.cancel('job', expected_revision=waiting['revision'])['state'] == 'cancelled'
    job = running(store, 'unknown')
    unknown = store.transition('unknown', 'outcome_unknown', **credentials(job))
    assert store.cancel('unknown', expected_revision=unknown['revision'])['state'] == 'outcome_unknown'


def test_usage_persists_across_pause_wait_restart_and_resume(stores, clock):
    store = stores()
    job = running(store)
    charged = store.reserve_usage('job', **credentials(job), actions=2, active_ms=1000, progress=False)
    waiting = store.transition('job', 'waiting_approval', **credentials(charged))
    clock[0] += 1000000
    reopened = stores()
    reopened.resume('job', expected_revision=waiting['revision'])
    resumed = reopened.claim('next')
    assert (resumed['actions_used'],resumed['active_ms'],resumed['consecutive_no_progress']) == (2,1000,1)
    charged = reopened.reserve_usage('job', **credentials(resumed), actions=1, active_ms=500, progress=True)
    assert (charged['actions_used'],charged['active_ms'],charged['consecutive_no_progress']) == (3,1500,0)


@pytest.mark.parametrize('usage', [{'actions':25}, {'active_ms':300000}])
def test_budget_limit_prevents_more_work_resume_or_expired_retry(stores, clock, usage):
    store = stores()
    job = running(store)
    charged = store.reserve_usage('job', **credentials(job), **usage)
    with pytest.raises(BudgetExhausted):
        store.reserve_usage('job', **credentials(charged), actions=1, active_ms=1)
    with pytest.raises(BudgetExhausted):
        store.transition('job', 'queued', **credentials(charged))
    clock[0] = charged['lease_until_ms']
    assert store.recover_expired()[0]['state'] == 'failed'
    assert store.claim('next') is None


def test_no_progress_cannot_be_reset_to_evade_handoff(stores):
    store = stores()
    job = running(store)
    for _ in range(3):
        job = store.reserve_usage('job', **credentials(job), progress=False)
    with pytest.raises(BudgetExhausted):
        store.reserve_usage('job', **credentials(job), progress=True)
    waiting = store.transition('job', 'waiting_user', **credentials(job))
    with pytest.raises(BudgetExhausted):
        store.resume('job', expected_revision=waiting['revision'])


def test_claim_and_usage_rollback_do_not_leak_partial_state(stores):
    store = stores()
    original = store.enqueue('extract', {}, job_id='job')
    with pytest.raises(RuntimeError):
        with store.assistant.transaction():
            job = store.claim('worker')
            store.reserve_usage('job', **credentials(job), actions=1)
            raise RuntimeError('crash')
    assert stores().get('job') == original
    assert stores().claim('next')['attempts'] == 1


@pytest.mark.parametrize('bad', [True,-1,1.5,float('nan'),9007199254740992])
def test_invalid_times_and_cas_rejected_without_changes(stores, clock, bad):
    store = stores()
    job = running(store)
    with pytest.raises(ValueError):
        store.reserve_usage('job', **credentials(job), active_ms=bad)
    with pytest.raises(ValueError):
        store.cancel('job', expected_revision=bad)
    with pytest.raises(ValueError):
        store.heartbeat('job', **credentials(job), lease_ms=bad)
    clock[0] = bad
    with pytest.raises(ValueError):
        store.claim('next')
    assert store.get('job') == job
