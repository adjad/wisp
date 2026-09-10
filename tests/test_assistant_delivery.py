"""Offline event receipt and native action contracts; no native apps or model calls."""
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault('WISP_HOME', tempfile.mkdtemp(prefix='wisp-delivery-import-'))

import pytest
from httpx import ASGITransport, AsyncClient
from service.assistant.store import AssistantStore
from service.assistant.hub import Hub
from service.assistant import outbox, scheduler, brief
from service.assistant.reminders import due_reminders
from service.assistant.outbound_queue import OutboundQueue
from service.tools import assistant_tools
from service import main


def claim_event(store, event):
    payload = {k: v for k, v in event.items() if k != 'event_id'}
    return store.claim_calendar_action(event['event_id'], event['type'], event['action_id'], payload)


@pytest.fixture
def world(tmp_path, monkeypatch):
    store = AssistantStore(tmp_path / 'assistant.db')
    hub = Hub(store)
    for module in (main, scheduler, brief, assistant_tools):
        monkeypatch.setattr(module, 'assistant_store', store)
    monkeypatch.setattr(main, 'assistant_hub', hub)
    monkeypatch.setattr(scheduler, 'hub', hub)
    monkeypatch.setattr(outbox, 'hub', hub)
    import importlib
    monkeypatch.setattr(importlib.import_module('service.assistant.hub'), 'hub', hub)
    yield store, hub
    store._db.close()


@pytest.mark.asyncio
async def test_no_subscribers_restart_replay_and_duplicate_ack(world):
    store, hub = world
    one = await hub.publish({'type': 'daily_brief', 'text': 'fixture'}, dedupe_key='brief',
                            target={'type': 'brief', 'date': '2026-09-10'})
    two = await hub.publish({'type': 'daily_brief', 'text': 'recomposed'}, dedupe_key='brief')
    assert one == two
    assert store.completion('daily_brief') is None
    restarted = AssistantStore(store.path)
    stream = Hub(restarted).events()
    replay = await anext(stream)
    assert replay == one
    assert restarted.event(one['event_id'])['attempts'] == 1
    with pytest.raises(ValueError):
        restarted.acknowledge_event(one['event_id'], 'reminder')
    assert restarted.acknowledge_event(one['event_id'], 'daily_brief')
    assert restarted.acknowledge_event(one['event_id'], 'daily_brief')
    assert restarted.pending_events() == []
    assert restarted.completion('daily_brief') == '2026-09-10'
    await stream.aclose()
    restarted._db.close()


@pytest.mark.asyncio
async def test_replay_live_boundary_has_no_connection_duplicates(world):
    store, hub = world
    first = await hub.publish({'type': 'changed'}, dedupe_key='first')
    stream = hub.events()
    assert await anext(stream) == first
    await hub.publish(first)
    second = await hub.publish({'type': 'changed'}, dedupe_key='second')
    assert await asyncio.wait_for(anext(stream), 1) == second
    await stream.aclose()
    reconnect = hub.events()
    assert await anext(reconnect) == first
    assert await anext(reconnect) == second
    await reconnect.aclose()


def test_reminder_stages_only_finalize_on_receipt_and_schedule_guard(world):
    store, _ = world
    now = time.time()
    c = store.add_manual('Fixture exam', now + 100, kind='exam')
    notes = due_reminders(store, now)
    assert len(notes) == 1
    assert not store.already_notified(c['id'], 'T-3h')
    assert not store.already_notified(c['id'], 'T-1d')
    assert due_reminders(store, now + 1) == []
    store.acknowledge_event(notes[0]['event_id'], 'reminder')
    for stage in ('T-3h', 'T-1d', 'T-1w'):
        assert store.already_notified(c['id'], stage)
    store.update_schedule([c['id']], now + 200)
    next_note = due_reminders(store, now)[0]
    store.update_schedule([c['id']], now + 400)
    store.acknowledge_event(next_note['event_id'], 'reminder')
    assert not store.already_notified(c['id'], 'T-3h')


def test_unknown_notice_receipt_is_same_transaction(world):
    store, _ = world
    queue = OutboundQueue(store.path)
    sid = queue.add(channel='message', recipient='fixture', body='fixture', when_ts=time.time())
    queue.claim(sid)
    queue.recover_in_flight()
    row = store.enqueue_event({'type': 'scheduled_send_unknown', 'notice_id': sid},
                              target={'type': 'scheduled_unknown', 'id': sid})
    assert store.acknowledge_event(row['id'], row['kind'])
    assert queue.unacknowledged_unknown() == []
    assert store.acknowledge_event(row['id'], row['kind'])
    invalid = store.enqueue_event({'type': 'scheduled_send_unknown'},
                                 target={'type': 'scheduled_unknown', 'id': 'missing'})
    with pytest.raises(ValueError):
        store.acknowledge_event(invalid['id'], invalid['kind'])
    assert store.event(invalid['id'])['state'] == 'pending'
    queue._db.close()


@pytest.mark.asyncio
async def test_calendar_disconnected_timeout_and_expired_claim(world):
    store, hub = world
    assert not (await outbox.request('create_calendar_event', {'title': 'fixture'}))['ok']
    assert store.pending_events() == []
    q = hub.subscribe()
    result = await outbox.request('create_calendar_event', {'title': 'fixture', 'when_ts': time.time() + 900, 'duration_min': 60, 'location': ''}, timeout=.01)
    event = await q.get()
    assert result['ok'] is False and result['status'] == 'unknown'
    claim = claim_event(store, event)
    assert claim['execute'] is False and not claim['result']['ok']
    assert store.active_between() == []
    hub.unsubscribe(q)


@pytest.mark.asyncio
@pytest.mark.parametrize('success', [True, False])
async def test_calendar_result_and_duplicate_reconciliation(world, success):
    store, hub = world
    c = {'source_id': 'native-fixture', 'title': 'Fixture meeting', 'when_ts': time.time() + 1000}
    store.sync_source('calendar', [c])
    original = store.active_between()[0]
    q = hub.subscribe()
    waiting = asyncio.create_task(outbox.request('delete_calendar_event', {
        'source_id': c['source_id'], 'when_ts': c['when_ts']}, timeout=1))
    event = await q.get()
    assert store.get(original['id'])['status'] == 'active'
    claim = claim_event(store, event)
    assert claim['execute']
    assert not claim_event(store, event)['execute']
    result = {'ok': success, 'status': 'succeeded' if success else 'failed', 'error': '' if success else 'access denied'}
    body = {'result': result, 'action_id': event['action_id'], 'event_id': event['event_id'],
            'kind': event['type'], 'claim_token': claim['claim_token']}
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        bad = await client.post('/assistant/action_result', json={**body, 'claim_token': 'wrong'})
        assert 400 <= bad.status_code < 500
        for _ in range(2):
            response = await client.post('/assistant/action_result', json=body)
            assert response.status_code == 200 and response.json()['ok']
        ack = await client.post(f"/assistant/events/{event['event_id']}/ack",
                                json={'kind': event['type'], 'state': 'handled'})
        assert ack.status_code == 200
        conflict = await client.post('/assistant/action_result', json={**body, 'result': {'ok': not success, 'status': 'failed' if success else 'succeeded', 'error': 'denied' if success else ''}})
        assert 400 <= conflict.status_code < 500
    assert (await waiting)['ok'] is success
    assert store.get(original['id'])['status'] == ('dismissed' if success else 'active')
    hub.unsubscribe(q)


@pytest.mark.asyncio
async def test_late_creation_result_survives_restart_and_is_idempotent(world):
    store, hub = world
    q = hub.subscribe()
    waiting = asyncio.create_task(outbox.request('create_calendar_event', {
        'title': 'Fixture lunch', 'when_ts': time.time() + 900, 'location': '', 'duration_min': 60}, timeout=.02))
    event = await q.get()
    claim = claim_event(store, event)
    assert (await waiting)['status'] == 'unknown'
    restarted = AssistantStore(store.path)
    args = (event['event_id'], event['type'], claim['claim_token'],
            {'ok': True, 'status': 'succeeded', 'source_id': 'native-created', 'error': ''})
    assert restarted.complete_calendar_action(*args)
    assert restarted.complete_calendar_action(*args)
    assert len(restarted.active_between()) == 1
    assert restarted.active_between()[0]['source_id'] == 'native-created'
    restarted._db.close()
    hub.unsubscribe(q)


@pytest.mark.asyncio
async def test_ack_api_identity_and_source_allowlist_no_mutation(world):
    store, hub = world
    event = await hub.publish({'type': 'changed'})
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        for label in ('email', 'contacts', 'notes', 'browser_history', 'wisp_seed', '', None, []):
            response = await client.post('/assistant/sync/calendar', json={'source': label, 'events': []})
            assert response.status_code == 422
        assert store.active_between() == []
        for label in ('Calendar', 'apple_calendar', 'Apple Reminders', 'reminders', 'Wisp', 'manual'):
            response = await client.post('/assistant/sync/calendar', json={'source': label, 'events': []})
            assert response.status_code == 200
        path = f"/assistant/events/{event['event_id']}/ack"
        assert (await client.post(path, json={'kind': 'reminder', 'state': 'handled'})).status_code == 409
        assert (await client.post(path, json={'kind': 'changed', 'state': 'received'})).status_code == 422
        assert (await client.post('/assistant/events/unknown/ack', json={'kind': 'changed', 'state': 'handled'})).status_code == 404
        for _ in range(2):
            assert (await client.post(path, json={'kind': 'changed', 'state': 'handled'})).json()['ok']


@pytest.mark.asyncio
async def test_sse_endpoint_replays_persisted_event(world):
    _, hub = world
    event = await hub.publish({'type': 'changed'})
    response = await main.assistant_events()
    stream = response.body_iterator
    assert json.loads((await anext(stream)).removeprefix('data: '))['type'] == 'hello'
    assert json.loads((await anext(stream)).removeprefix('data: ')) == event
    await stream.aclose()


@pytest.mark.asyncio
async def test_calendar_tool_and_delete_api_do_not_retire_before_success(world, monkeypatch):
    store, _ = world
    store.sync_source('calendar', [{'source_id': 'fixture', 'title': 'Fixture lunch', 'when_ts': time.time() + 900}])
    row = store.active_between()[0]
    monkeypatch.setattr(outbox, 'request', AsyncMock(return_value={'ok': False, 'error': 'native denied'}))
    assert 'error' in await assistant_tools.cancel_event('Fixture lunch')
    assert not (await main.assistant_delete(row['id']))['ok']
    assert store.get(row['id'])['status'] == 'active'
    assert 'error' in await assistant_tools.add_calendar_event('Fixture', '2099-09-10T09:00')
    assert len(store.active_between()) == 1


def test_pending_reminder_coalesces_and_cancelled_schedule_never_replays(world):
    store, _ = world
    now = time.time()
    row = store.add_manual('Fixture meeting', now + 1700, kind='meeting')
    first = due_reminders(store, now)[0]
    newer = due_reminders(store, now + 1200)[0]
    assert store.event(first['event_id'])['state'] == 'superseded'
    assert [e['id'] for e in store.pending_events()] == [newer['event_id']]
    store.set_status(row['id'], 'dismissed')
    assert store.pending_events() == []
    assert not store.event_attempt(newer['event_id'])
    assert not store.already_notified(row['id'], 'T-10m')


def test_rescheduled_reminder_drops_old_payload_and_mirror_receipt_matches_seconds(world):
    store, _ = world
    now = int(time.time() // 60) * 60 + 40
    row = store.add_manual('Fixture alarm', now - 3)
    store.sync_source('reminders', [{'source_id': 'mirror', 'kind': 'reminder',
                                   'title': 'Fixture alarm', 'when_ts': now - 40}])
    twin = store.duplicate_ids(row['id'])[0]
    event = due_reminders(store, now)[0]
    store.acknowledge_event(event['event_id'], 'reminder')
    assert store.already_notified(row['id'], 'due')
    assert store.already_notified(twin, 'due')
    row2 = store.add_manual('Move fixture', now + 100, kind='meeting')
    old = due_reminders(store, now)[0]
    store.update_schedule([row2['id']], now + 10000)
    assert not store.event_attempt(old['event_id'])
    assert store.event(old['event_id'])['state'] == 'superseded'


@pytest.mark.asyncio
async def test_legacy_mirror_and_outbound_commands_do_not_become_replayable(world):
    store, hub = world
    await hub.publish({'type': 'create_apple_reminder', 'title': 'fixture'})
    assert store.pending_events() == []
    result = await outbox.request('send_message', {'to': 'fixture', 'text': 'never sent'}, timeout=.001)
    assert not result['ok']
    assert store.pending_events() == []


def test_event_schema_addition_preserves_legacy_data_and_reopens(world):
    store, _ = world
    row = store.add_manual('Preserved fixture', time.time() + 1000)
    store.mark_notified(row['id'], 'due')
    # A pre-event-outbox database has the same commitment schema and logs.
    store._db.execute('DROP TABLE assistant_events')
    store._db.execute('DROP TABLE assistant_completion')
    store._db.commit()
    reopened = AssistantStore(store.path)
    assert reopened.get(row['id']) == store.get(row['id'])
    assert reopened.already_notified(row['id'], 'due')
    event = reopened.enqueue_event({'type': 'changed'}, dedupe_key='preserve')
    again = AssistantStore(store.path)
    assert again.enqueue_event({'type': 'changed'}, dedupe_key='preserve')['id'] == event['id']
    reopened._db.close()
    again._db.close()


def test_ack_failure_rolls_back_target_completion(world):
    import sqlite3
    store, _ = world
    now = time.time()
    c = store.add_manual('Transaction fixture', now - 1)
    event = due_reminders(store, now)[0]
    store._db.execute("""CREATE TRIGGER reject_event_ack BEFORE UPDATE OF state ON assistant_events
        WHEN NEW.state='ack' BEGIN SELECT RAISE(ABORT, 'fixture disk failure'); END""")
    store._db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.acknowledge_event(event['event_id'], 'reminder')
    assert not store.already_notified(c['id'], 'due')
    assert store.event(event['event_id'])['state'] == 'pending'


def test_return_to_previous_schedule_uses_new_delivery_generation(world):
    store, _ = world
    now = time.time()
    row = store.add_manual('Schedule fixture', now + 100, kind='meeting')
    original = due_reminders(store, now)[0]
    store.update_schedule([row['id']], now + 10000)
    assert store.pending_events() == []
    store.update_schedule([row['id']], row['when_ts'])
    replay = due_reminders(store, now)[0]
    assert replay['event_id'] != original['event_id']
    store.acknowledge_event(replay['event_id'], 'reminder')
    assert store.already_notified(row['id'], 'T-10m')


def calendar_row(store, suffix='fixture', kind='delete_calendar_event'):
    payload = {'type': kind, 'action_id': 'action-' + suffix, 'when_ts': time.time() + 900}
    if kind == 'delete_calendar_event':
        payload['source_id'] = 'native-' + suffix
        store.sync_source('calendar', [{'source_id': payload['source_id'], 'title': 'Fixture', 'when_ts': payload['when_ts']}])
    else:
        payload.update(title='Fixture', duration_min=60, location='')
    row = store.enqueue_event(payload, target={'type': 'calendar'}, dedupe_key='action:' + payload['action_id'], expires_at=time.time() + 60)
    return row, {**payload, 'event_id': row['id']}


def snapshot(store, event_id):
    return (store.event(event_id), [dict(r) for r in store._db.execute('SELECT * FROM commitments ORDER BY id')],
            [tuple(r) for r in store._db.execute('SELECT * FROM notify_log ORDER BY commitment_id,stage')])


@pytest.mark.parametrize('round_number', range(25))
def test_f1_two_connection_claim_race(world, round_number):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store, _ = world
    other = AssistantStore(store.path)
    row, event = calendar_row(store, str(round_number))
    barrier = Barrier(2)
    def contender(connection):
        barrier.wait(timeout=3)
        return claim_event(connection, event)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            grants = list(pool.map(contender, (store, other)))
        assert sum(g['execute'] is True for g in grants) == 1
        winner = next(g for g in grants if g['execute'])
        loser = next(g for g in grants if not g['execute'])
        assert not loser['recorded'] and 'claim_token' not in loser
        reopened = AssistantStore(store.path)
        assert reopened.event(row['id'])['claim_token'] == winner['claim_token']
        assert not claim_event(reopened, event)['execute']
        reopened._db.close()
        result = {'ok': True, 'status': 'succeeded', 'error': ''}
        assert store.complete_calendar_action(row['id'], row['kind'], winner['claim_token'], result)
        assert claim_event(other, event)['result'] == result
    finally:
        other._db.close()


@pytest.mark.parametrize('round_number', range(20))
def test_f2_ack_reschedule_two_connection_race(world, round_number):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store, _ = world
    other = AssistantStore(store.path)
    now = time.time()
    c = store.add_manual('Concurrent fixture', now - 1)
    event = due_reminders(store, now)[0]
    barrier = Barrier(2)
    def ack():
        barrier.wait(timeout=3)
        return store.acknowledge_event(event['event_id'], 'reminder')
    def move():
        barrier.wait(timeout=3)
        return other.update_schedule([c['id']], now + 7200)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(ack), pool.submit(move)
            assert a.result(timeout=5) is True
            assert b.result(timeout=5) == 1
        assert not store.already_notified(c['id'], 'due')
        assert store.get(c['id'])['when_ts'] == now + 7200
        for _ in range(2):
            assert other.acknowledge_event(event['event_id'], 'reminder')
            assert not store.already_notified(c['id'], 'due')
    finally:
        other._db.close()


@pytest.mark.parametrize('first', ['ack', 'reschedule'])
def test_f2_validation_holds_database_lock_until_completion(world, first):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    store, _ = world
    other = AssistantStore(store.path)
    now = time.time()
    c = store.add_manual('Serialized fixture', now - 1)
    event = due_reminders(store, now)[0]
    locked, release, contender_started = Event(), Event(), Event()
    first_store = store if first == 'ack' else other
    # A real SQLite trace pauses AFTER BEGIN IMMEDIATE acquired its lock. No
    # barrier inside SELECT requires the losing connection to acquire that lock.
    paused = False
    def trace(sql):
        nonlocal paused
        if not paused and ((first == 'ack' and sql.startswith('SELECT * FROM assistant_events'))
                           or (first == 'reschedule' and sql.startswith('UPDATE commitments'))):
            paused = True
            locked.set()
            assert release.wait(timeout=3)
    first_store._db.set_trace_callback(trace)
    def ack(): return store.acknowledge_event(event['event_id'], 'reminder')
    def move(): return other.update_schedule([c['id']], now + 5000)
    def second():
        contender_started.set()
        return move() if first == 'ack' else ack()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            leading = pool.submit(ack if first == 'ack' else move)
            assert locked.wait(timeout=3)
            trailing = pool.submit(second)
            assert contender_started.wait(timeout=3)
            # Query the lock independently; failure proves serialization rather
            # than merely relying on thread scheduling or a sleep assertion.
            import sqlite3
            probe = sqlite3.connect(store.path, timeout=0)
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                probe.execute('BEGIN IMMEDIATE')
            probe.close()
            release.set()
            leading.result(timeout=5)
            trailing.result(timeout=5)
        assert not store.already_notified(c['id'], 'due')
        assert store.acknowledge_event(event['event_id'], 'reminder')
        assert not store.already_notified(c['id'], 'due')
    finally:
        release.set()
        first_store._db.set_trace_callback(None)
        other._db.close()


BAD_RESULTS = [None, [], 'success', {}, {'status': 'succeeded', 'error': ''},
    {'ok': 'true', 'status': 'succeeded', 'error': ''},
    {'ok': 1, 'status': 'succeeded', 'error': ''},
    {'ok': 0, 'status': 'failed', 'error': 'denied'},
    {'ok': True, 'status': 'succeeded', 'error': 'native operation failed'},
    {'ok': True, 'error': ''}, {'ok': True, 'status': 'failed', 'error': ''},
    {'ok': True, 'status': 1, 'error': ''}, {'ok': True, 'status': 'succeeded', 'error': None},
    {'ok': False, 'status': 'failed', 'error': ''},
    {'ok': True, 'status': 'succeeded', 'error': '', 'unexpected': []}]


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_result', BAD_RESULTS)
async def test_f3_malformed_result_is_retryable_without_mutation(world, bad_result):
    store, _ = world
    row, event = calendar_row(store)
    claim = claim_event(store, event)
    body = {'event_id': row['id'], 'action_id': event['action_id'], 'kind': row['kind'], 'claim_token': claim['claim_token']}
    before = snapshot(store, row['id'])
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        response = await client.post('/assistant/action_result', json={**body, 'result': bad_result})
        assert 400 <= response.status_code < 500
        assert snapshot(store, row['id']) == before
        corrected = {**body, 'result': {'ok': True, 'status': 'succeeded', 'error': ''}}
        for _ in range(2):
            response = await client.post('/assistant/action_result', json=corrected)
            assert response.status_code == 200
            assert response.json()['recorded'] is True
            assert response.json()['event_id'] == row['id']
            assert response.json()['action_id'] == event['action_id']
        assert store.event(row['id'])['result'] == corrected['result']
        assert store._db.execute("SELECT status FROM commitments").fetchone()[0] == 'dismissed'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('action_id', None), ('action_id', 1), ('action_id', ''), ('action_id', 'wrong'),
    ('event_id', None), ('event_id', 'wrong'), ('kind', []), ('kind', 'wrong'), ('claim_token', ''), ('claim_token', 'wrong'), ('result', 'missing')])
async def test_f3_f6_invalid_envelope_does_not_poison_result(world, field, value):
    store, _ = world
    row, event = calendar_row(store)
    claim = claim_event(store, event)
    good = {'event_id': row['id'], 'action_id': event['action_id'], 'kind': row['kind'],
            'claim_token': claim['claim_token'], 'result': {'ok': True, 'status': 'succeeded', 'error': ''}}
    bad = {**good, field: value}
    if value is None or value == 'missing': bad.pop(field)
    before = snapshot(store, row['id'])
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        response = await client.post('/assistant/action_result', json=bad)
        assert 400 <= response.status_code < 500
        assert snapshot(store, row['id']) == before
        assert (await client.post('/assistant/action_result', json=good)).json()['recorded'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['action', 'title', 'time_bool', 'duration_bool', 'missing', 'kind_list'])
async def test_f6_claim_binds_action_and_exact_payload_before_execution(world, mutation):
    store, _ = world
    row, event = calendar_row(store, kind='create_calendar_event')
    payload = row['payload'].copy()
    good = {'kind': row['kind'], 'action_id': event['action_id'], 'payload': payload.copy()}
    bad = {**good, 'payload': payload}
    if mutation == 'action': bad['action_id'] = 'other'
    elif mutation == 'title': payload['title'] = 'Different target'
    elif mutation == 'time_bool': payload['when_ts'] = True
    elif mutation == 'duration_bool': payload['duration_min'] = True
    elif mutation == 'missing': payload.pop('title')
    else: bad['kind'] = []
    before = snapshot(store, row['id'])
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        response = await client.post(f"/assistant/events/{row['id']}/claim", json=bad)
        assert 400 <= response.status_code < 500
        assert snapshot(store, row['id']) == before
        response = await client.post(f"/assistant/events/{row['id']}/claim", json=good)
        assert response.json()['execute'] is True
        assert response.json()['payload'] == row['payload']


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['reminderſ', 'wiſp', 'cаlendar', 'ｃalendar', '\u00a0Wisp', 'Wisp\u00a0', 'unknown'])
async def test_f4_confusable_source_never_replaces_rows(world, source):
    store, _ = world
    for label in ('calendar', 'reminders', 'manual'):
        store.sync_source(label, [{'source_id': label, 'title': label, 'when_ts': 1234567890}])
    before = [dict(r) for r in store._db.execute('SELECT * FROM commitments ORDER BY id')]
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        response = await client.post('/assistant/sync/calendar', json={'source': source, 'events': []})
    assert response.status_code == 422
    assert [dict(r) for r in store._db.execute('SELECT * FROM commitments ORDER BY id')] == before


def test_f1_result_and_f2_reschedule_roll_back_all_state(world):
    import sqlite3
    store, _ = world
    row, event = calendar_row(store)
    claim = claim_event(store, event)
    before = snapshot(store, row['id'])
    store._db.execute("CREATE TRIGGER fail_result BEFORE UPDATE OF result ON assistant_events BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.complete_calendar_action(row['id'], row['kind'], claim['claim_token'], {'ok': True, 'status': 'succeeded', 'error': ''})
    assert snapshot(store, row['id']) == before
    store._db.execute('DROP TRIGGER fail_result')
    c = store.add_manual('Rollback revision', time.time() - 1)
    note = due_reminders(store)[0]
    before = snapshot(store, note['event_id'])
    generation = store.reminder_generation(c['id'])
    store._db.execute("CREATE TRIGGER fail_supersede BEFORE UPDATE OF state ON assistant_events WHEN NEW.state='superseded' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.update_schedule([c['id']], time.time() + 9000)
    assert snapshot(store, note['event_id']) == before
    assert store.reminder_generation(c['id']) == generation


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [{}, {'ok': 'true'}, {'ok': 1}, {'ok': True, 'error': 'native operation failed'}])
async def test_f3_exact_legacy_flat_audit_envelopes_reject_then_correct(world, legacy):
    store, _ = world
    row, event = calendar_row(store)
    claim = claim_event(store, event)
    identity = {'event_id': row['id'], 'action_id': event['action_id'], 'kind': row['kind'], 'claim_token': claim['claim_token']}
    before = snapshot(store, row['id'])
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        response = await client.post('/assistant/action_result', json={**identity, **legacy})
        assert 400 <= response.status_code < 500
        assert snapshot(store, row['id']) == before
        result = {'ok': True, 'status': 'succeeded', 'error': ''}
        assert (await client.post('/assistant/action_result', json={**identity, 'result': result})).json()['recorded'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['failed', 'unknown'])
async def test_f3_valid_negative_result_is_terminal_without_commitment_mutation(world, status):
    store, _ = world
    row, event = calendar_row(store)
    claim = claim_event(store, event)
    before = snapshot(store, row['id'])[1:]
    result = {'ok': False, 'status': status, 'error': 'native denied or unconfirmed'}
    body = {'event_id': row['id'], 'action_id': event['action_id'], 'kind': row['kind'], 'claim_token': claim['claim_token'], 'result': result}
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        for _ in range(2):
            assert (await client.post('/assistant/action_result', json=body)).json()['recorded'] is True
    assert snapshot(store, row['id'])[1:] == before
    assert store.event(row['id'])['result'] == result


@pytest.mark.asyncio
@pytest.mark.parametrize('label,source', [('Calendar', 'calendar'), ('APPLE CALENDAR', 'calendar'), ('apple_calendar', 'calendar'),
    ('Reminders', 'reminders'), ('APPLE REMINDERS', 'reminders'), ('apple_reminders', 'reminders'), ('Wisp', 'manual'), ('manual', 'manual')])
async def test_f4_ascii_alias_only_replaces_its_source(world, label, source):
    store, _ = world
    for canonical in ('calendar', 'reminders', 'manual'):
        store.sync_source(canonical, [{'source_id': canonical, 'title': canonical, 'when_ts': 1234567890}])
    before = [dict(r) for r in store._db.execute('SELECT * FROM commitments WHERE source<>? ORDER BY id', (source,))]
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        assert (await client.post('/assistant/sync/calendar', json={'source': label, 'events': []})).status_code == 200
    after = [dict(r) for r in store._db.execute('SELECT * FROM commitments ORDER BY id')]
    assert after == before


def test_f1_claim_write_failure_does_not_leave_a_grant(world):
    import sqlite3
    store, _ = world
    row, event = calendar_row(store)
    before = snapshot(store, row['id'])
    store._db.execute("CREATE TRIGGER fail_claim BEFORE UPDATE OF claim_token ON assistant_events BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError): claim_event(store, event)
    assert snapshot(store, row['id']) == before
    store._db.execute('DROP TRIGGER fail_claim')
    assert claim_event(store, event)['execute'] is True


@pytest.mark.parametrize('kind,legacy', [('delete_calendar_event', {'ok': True, 'error': ''}),
    ('create_calendar_event', {'ok': True, 'error': '', 'source_id': 'native-legacy'}),
    ('delete_calendar_event', {'ok': False, 'error': 'denied'}),
    ('delete_calendar_event', {'ok': False, 'error': 'Wisp was interrupted; native Calendar outcome is unknown'})])
def test_f3_valid_legacy_results_migrate_without_repeating_effects(world, kind, legacy):
    store, _ = world
    row, event = calendar_row(store, kind=kind)
    claim_event(store, event)
    store._db.execute('UPDATE assistant_events SET result=? WHERE id=?', (json.dumps(legacy), row['id']))
    store._db.commit()
    before = snapshot(store, row['id'])[1:]
    reopened = AssistantStore(store.path)
    result = claim_event(reopened, event)
    assert result['execute'] is False and result['recorded'] is True
    status = ('succeeded' if legacy['ok'] else 'unknown'
              if legacy['error'] == 'Wisp was interrupted; native Calendar outcome is unknown' else 'failed')
    assert result['result'] == {**legacy, 'status': status}
    assert reopened.acknowledge_event(row['id'], kind)
    assert snapshot(reopened, row['id'])[1:] == before
    reopened._db.close()


def test_f3_ambiguous_legacy_result_is_preserved_without_execution_grant(world):
    store, _ = world
    row, event = calendar_row(store)
    claim_event(store, event)
    bad = json.dumps({'ok': True, 'error': 'native operation failed'})
    store._db.execute('UPDATE assistant_events SET result=? WHERE id=?', (bad, row['id']))
    store._db.commit()
    reopened = AssistantStore(store.path)
    with pytest.raises(ValueError): claim_event(reopened, event)
    assert reopened._db.execute('SELECT result FROM assistant_events WHERE id=?', (row['id'],)).fetchone()[0] == bad
    reopened._db.close()


@pytest.mark.asyncio
async def test_f6_stored_payload_identity_must_match_result_action(world):
    store, _ = world
    row, event = calendar_row(store)
    claim = claim_event(store, event)
    altered = {**row['payload'], 'action_id': 'different'}
    store._db.execute('UPDATE assistant_events SET payload=? WHERE id=?', (json.dumps(altered), row['id']))
    store._db.commit()
    before = snapshot(store, row['id'])
    body = {'event_id': row['id'], 'action_id': event['action_id'], 'kind': row['kind'], 'claim_token': claim['claim_token'],
            'result': {'ok': True, 'status': 'succeeded', 'error': ''}}
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        assert (await client.post('/assistant/action_result', json=body)).status_code == 409
    assert snapshot(store, row['id']) == before


@pytest.mark.parametrize('boundary', ['snapshot', 'enqueue'])
@pytest.mark.parametrize('change', ['move', 'same_time', 'aba', 'twin', 'title', 'kind', 'context', 'dismiss', 'delete', 'insert'])
def test_f7_prepared_snapshot_rejects_concurrent_changes(world, monkeypatch, boundary, change):
    store, _ = world
    other = AssistantStore(store.path)
    now = int(time.time() // 60) * 60 + 40
    original_when, future_when = now - 3, now + 86400
    primary = store.add_manual('Snapshot fixture', original_when)
    store.sync_source('reminders', [{'source_id': 'snapshot-twin', 'kind': 'reminder',
                                   'title': 'Snapshot fixture', 'when_ts': now - 40}])
    twin = store.duplicate_ids(primary['id'])[0]
    expected = None

    def state():
        return tuple([tuple(r) for r in store._db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                     for table in ('assistant_events', 'notify_log'))

    def mutate():
        nonlocal expected
        if change == 'move': other.update_schedule([primary['id'], twin], future_when)
        elif change == 'same_time': other.update_schedule([primary['id']], original_when)
        elif change == 'aba':
            other.update_schedule([primary['id']], future_when)
            other.update_schedule([primary['id']], original_when)
        elif change == 'twin': other.update_schedule([twin], future_when)
        elif change == 'title': other.update_schedule([primary['id']], original_when, title='Renamed fixture')
        elif change in ('kind', 'context'):
            other._db.execute(f'UPDATE commitments SET {change}=? WHERE id=?',
                              ('meeting' if change == 'kind' else 'Changed context', primary['id']))
            other._db.commit()
        elif change == 'dismiss': other.set_status(primary['id'], 'dismissed')
        elif change == 'insert': other.add_manual('Snapshot fixture', now - 2, kind='meeting')
        else: other.delete(primary['id'])
        expected = state()

    try:
        # Both hooks run after real reads/preparation and mutate through a
        # genuinely independent connection, never inside the writer's lock.
        with monkeypatch.context() as patch:
            if boundary == 'snapshot':
                original = store.reminder_snapshot
                def captured(at):
                    rows = original(at)
                    mutate()
                    return rows
                patch.setattr(store, 'reminder_snapshot', captured)
            else:
                original = store.enqueue_event
                def prepared(*args, **kwargs):
                    mutate()
                    return original(*args, **kwargs)
                patch.setattr(store, 'enqueue_event', prepared)
            assert due_reminders(store, now) == []
        assert expected is not None and state() == expected
        assert not store.already_notified(primary['id'], 'due')
        assert not store.already_notified(twin, 'due')
        # A fresh snapshot still delivers the valid replacement. In particular
        # the future due stage must survive the stale preparation attempt.
        at = future_when if change in ('move', 'twin') else now
        fresh = due_reminders(store, at)
        assert fresh
        for event in fresh:
            assert store.event_attempt(event['event_id'])
            assert store.acknowledge_event(event['event_id'], 'reminder')
        if change in ('move', 'twin'):
            assert store.already_notified(twin, 'due')
    finally:
        other._db.close()


def test_f7_valid_snapshot_retains_each_native_twin_timestamp(world):
    store, _ = world
    now = int(time.time() // 60) * 60 + 40
    primary = store.add_manual('Valid snapshot fixture', now - 3)
    store.sync_source('reminders', [{'source_id': 'valid-twin', 'kind': 'reminder',
                                   'title': 'Valid snapshot fixture', 'when_ts': now - 40}])
    twin = store.duplicate_ids(primary['id'])[0]
    event = due_reminders(store, now)[0]
    target = store.event(event['event_id'])['target']
    assert event['when_ts'] == now - 3
    assert target['schedules'] == {primary['id']: now - 3, twin: now - 40}
    assert store.event_attempt(event['event_id'])
    assert store.acknowledge_event(event['event_id'], 'reminder')
    assert all(store.already_notified(cid, 'due') for cid in (primary['id'], twin))
    assert due_reminders(store, now) == []


@pytest.mark.parametrize('entry', ['attempt', 'replay', 'ack'])
@pytest.mark.parametrize('offset', [20, 86400])
def test_f7_historical_mixed_snapshot_cannot_finalize_replacement(world, entry, offset):
    store, _ = world
    now = int(time.time() // 60) * 60 + 20
    primary = store.add_manual('Historical snapshot fixture', now - 1)
    event = due_reminders(store, now)[0]
    old = store.event(event['event_id'])
    store.update_schedule([primary['id']], now + offset)
    mixed = {**old['target'], 'schedules': {primary['id']: now + offset},
             'generations': {primary['id']: store.reminder_generation(primary['id'])}}
    # Persist the exact inconsistent shape a previous version could produce.
    store._db.execute("UPDATE assistant_events SET target=?,state='pending' WHERE id=?",
                      (json.dumps(mixed), event['event_id']))
    store._db.commit()
    if entry == 'attempt': assert not store.event_attempt(event['event_id'])
    elif entry == 'replay': assert store.pending_events() == []
    else: assert store.acknowledge_event(event['event_id'], 'reminder')
    assert store.event(event['event_id'])['state'] == 'superseded'
    assert store.acknowledge_event(event['event_id'], 'reminder')
    assert not store.already_notified(primary['id'], 'due')
    replacement = due_reminders(store, now + offset)
    assert len(replacement) == 1 and replacement[0]['when_ts'] == now + offset
