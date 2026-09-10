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
    result = await outbox.request('create_calendar_event', {'title': 'fixture'}, timeout=.01)
    event = await q.get()
    assert result['ok'] is False and result['status'] == 'unknown'
    claim = store.claim_calendar_action(event['event_id'], event['type'])
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
    claim = store.claim_calendar_action(event['event_id'], event['type'])
    assert claim['execute']
    assert not store.claim_calendar_action(event['event_id'], event['type'])['execute']
    result = {'ok': success, 'error': '' if success else 'access denied'}
    body = {**result, 'action_id': event['action_id'], 'event_id': event['event_id'],
            'kind': event['type'], 'claim_token': claim['claim_token']}
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://fixture') as client:
        bad = await client.post('/assistant/action_result', json={**body, 'claim_token': 'wrong'})
        assert bad.status_code == 409
        for _ in range(2):
            response = await client.post('/assistant/action_result', json=body)
            assert response.status_code == 200 and response.json()['ok']
        ack = await client.post(f"/assistant/events/{event['event_id']}/ack",
                                json={'kind': event['type'], 'state': 'handled'})
        assert ack.status_code == 200
        conflict = await client.post('/assistant/action_result', json={**body, 'ok': not success})
        assert conflict.status_code == 409
    assert (await waiting)['ok'] is success
    assert store.get(original['id'])['status'] == ('dismissed' if success else 'active')
    hub.unsubscribe(q)


@pytest.mark.asyncio
async def test_late_creation_result_survives_restart_and_is_idempotent(world):
    store, hub = world
    q = hub.subscribe()
    waiting = asyncio.create_task(outbox.request('create_calendar_event', {
        'title': 'Fixture lunch', 'when_ts': time.time() + 900, 'location': ''}, timeout=.02))
    event = await q.get()
    claim = store.claim_calendar_action(event['event_id'], event['type'])
    assert (await waiting)['status'] == 'unknown'
    restarted = AssistantStore(store.path)
    args = (event['event_id'], event['type'], claim['claim_token'],
            {'ok': True, 'source_id': 'native-created', 'error': ''})
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
