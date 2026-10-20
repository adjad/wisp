"""Date-only update and stale-target controls with synthetic stores/receipts."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
import pytest


@pytest.fixture
def dates(tmp_path, monkeypatch):
    from service.assistant.store import AssistantStore
    from service.tools import assistant_tools as tools
    store = AssistantStore(tmp_path / 'reminders.sqlite')
    monkeypatch.setattr(tools, 'assistant_store', store)
    yield store, tools
    store._db.close()


@pytest.mark.asyncio
async def test_manual_day_change_preserves_date_only_and_explicit_clock_converts(dates):
    from service.assistant.verified_reminders import due_timestamp
    store, tools = dates
    date = (datetime.now() + timedelta(days=4)).date().isoformat()
    row = store.add_manual('Review report', due_timestamp({'all_day': True, 'due_date': date}), all_day=True, due_date=date)
    result = await tools.update_reminder(title='Review report', day='tomorrow', expected_id=row['id'])
    expected = (datetime.now() + timedelta(days=1)).date().isoformat()
    assert result.startswith('Reminder updated:') and 'all day' in result
    assert store.get(row['id'])['all_day'] and store.get(row['id'])['due_date'] == expected
    clock = expected + 'T15:00'
    result = await tools.update_reminder(title='Review report', when_iso=clock, expected_id=row['id'])
    assert result.startswith('Reminder updated:')
    updated = store.get(row['id'])
    assert not updated['all_day'] and updated['due_date'] is None
    assert datetime.fromtimestamp(updated['when_ts']).strftime('%H:%M') == '15:00'


@pytest.mark.asyncio
async def test_missing_all_day_flag_never_invents_a_midnight_alert(dates):
    store, tools = dates
    date = (datetime.now() + timedelta(days=4)).date().isoformat()
    result = await tools.add_reminder('Review report', date)
    assert 'nothing was added' in result
    row = store.add_manual('Review report', datetime.fromisoformat(date + 'T15:00').timestamp())
    result = await tools.update_reminder(title='Review report', when_iso=date, expected_id=row['id'])
    assert 'nothing was changed' in result
    assert datetime.fromtimestamp(store.get(row['id'])['when_ts']).hour == 15


@pytest.mark.asyncio
async def test_native_all_day_update_binds_old_date_and_requires_new_date_receipt(dates, monkeypatch):
    from service.assistant import outbox
    from service.assistant.verified_reminders import due_timestamp
    store, tools = dates
    old = (datetime.now() + timedelta(days=4)).date().isoformat()
    new = (datetime.now() + timedelta(days=5)).date().isoformat()
    store.sync_source('reminders', [{'source_id': 'synthetic:one', 'title': 'Review report', 'kind': 'reminder',
        'all_day': True, 'due_date': old, 'when_ts': due_timestamp({'all_day': True, 'due_date': old})}])
    request = AsyncMock(return_value={'ok': True, 'status': 'succeeded', 'error': '',
        'source_id': 'synthetic:one', 'title': 'Review report', 'all_day': True, 'due_date': old})
    monkeypatch.setattr(outbox, 'request', request)
    result = await tools.update_reminder(title='Review report', when_iso=new)
    assert 'not verified' in result
    kind, payload = request.await_args.args
    assert kind == 'update_reminder'
    assert payload['expected_due_date'] == old
    assert payload['all_day'] is True and payload['due_date'] == new
    assert 'due_ts' not in payload
