"""Actual /agent stream with isolated stores and synthetic effect boundaries."""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('dry_run', [True, False])
@pytest.mark.parametrize('all_day', [True, False])
async def test_two_reminders_reach_the_typed_entrypoint_without_model_or_native_calls(tmp_path, monkeypatch, dry_run, all_day):
    from service import main
    from service.assistant.store import AssistantStore
    from service.memory.store import SessionStore
    from service.tools.registry import REGISTRY, Tool
    from service.tasks import executor
    from service.assistant.verified_reminders import due_timestamp
    from service.safety.policy import Decision, Tier
    sessions = SessionStore(tmp_path / 'sessions.sqlite')
    reminders = AssistantStore(tmp_path / 'assistant.sqlite')
    monkeypatch.setattr(main, 'store', sessions)
    monkeypatch.setattr(main, 'assistant_store', reminders)
    monkeypatch.delenv('WISP_MODEL_LED_ROUTING', raising=False)
    monkeypatch.delenv('WISP_TYPED_REMINDERS_SHADOW_ONLY', raising=False)
    monkeypatch.setattr(main, 'ensure_omlx', AsyncMock(side_effect=AssertionError('model boundary reached')))
    monkeypatch.setattr(executor, 'decide', lambda *a, **k: Decision(Tier.ALLOW, 'synthetic test'))
    calls = []
    async def synthetic_create(title, when_iso, kind='reminder', all_day=False):
        calls.append(dict(title=title, when_iso=when_iso, all_day=all_day))
        ts = due_timestamp({'all_day': True, 'due_date': when_iso}) if all_day else datetime.fromisoformat(when_iso).timestamp()
        reminders.add_manual(title, ts, kind, **({'all_day': True, 'due_date': when_iso} if all_day else {}))
        return 'Reminder set: ' + title + ' in Wisp only; synthetic receipt.'
    original = REGISTRY['add_reminder']
    monkeypatch.setitem(REGISTRY, 'add_reminder', Tool('add_reminder', original.description,
        original.parameters, 'assistant_write', synthetic_create))
    first = (datetime.now() + timedelta(days=3)).date().isoformat()
    second = (datetime.now() + timedelta(days=5)).date().isoformat()
    prompt = (f'set {"all-day reminders" if all_day else "a reminder"} on {first} for review report '
              f'and another on {second} for microsoft and google earnings report')
    try:
        response = await main.agent({'prompt': prompt, 'test_mode': dry_run, 'debug': False})
        raw = b''
        async for chunk in response.body_iterator:
            raw += chunk if isinstance(chunk, bytes) else chunk.encode()
        events = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith('data: ')]
        plan = next(e for e in events if e['type'] == 'task_plan')
        assert plan['trace']['model_invocations'] == 0
        assert len([e for e in events if e['type'] == 'tool_call']) == 2
        assert events[-1]['type'] == 'done'
        main.ensure_omlx.assert_not_awaited()
        assert len(calls) == (0 if dry_run else 2)
        assert len(reminders.active_between(include_stale=True)) == len(calls)
        if calls:
            assert calls[1]['title'] == 'microsoft and google earnings report'
            assert calls[0]['when_iso'] == first if all_day else calls[0]['when_iso'].endswith('T09:00')
    finally:
        sessions._db.close()
        reminders._db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['legacy', 'model_led', 'planner', 'shadow'])
@pytest.mark.parametrize('correction', ['Actually, make it 4 PM.', 'Actually, 4 PM.',
    'Set it for 4 PM.', 'Move it to 25pm.', 'Could you change it to 4 PM?', 'Make both 4 PM.',
    'Push it to 4 PM.', 'Shift it to 4 PM.', 'Delay it by an hour.',
    'Move that reminder to 4 PM.', 'Set this reminder for 4 PM.',
    'Could you move it to 4 p.m.?', 'Move it to 4:7 PM.',
    'Actually, make it four PM.', 'Move the reminder to 4 PM.',
    "Make it 4 o'clock.", 'Move it to the 28th.'])
async def test_completed_batch_correction_cannot_fall_through_to_model_writes(tmp_path, monkeypatch, mode, correction):
    from service import main
    from service.assistant.store import AssistantStore
    from service.memory.store import SessionStore
    from service.tools.registry import REGISTRY, Tool
    from service.tasks import executor
    from service.assistant.verified_reminders import due_timestamp
    from service.safety.policy import Decision, Tier
    sessions = SessionStore(tmp_path / 'sessions.sqlite')
    reminders = AssistantStore(tmp_path / 'assistant.sqlite')
    monkeypatch.setattr(main, 'store', sessions)
    monkeypatch.setattr(main, 'assistant_store', reminders)
    for name in ['WISP_MODEL_LED_ROUTING', 'WISP_PLANNER', 'WISP_TYPED_REMINDERS_SHADOW_ONLY']:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main, 'ensure_omlx', AsyncMock(side_effect=AssertionError('model boundary reached')))
    monkeypatch.setattr(executor, 'decide', lambda *a, **k: Decision(Tier.ALLOW, 'synthetic test'))
    calls = []
    async def synthetic_create(title, when_iso, kind='reminder', all_day=False):
        calls.append(title)
        reminders.add_manual(title, due_timestamp({'all_day': True, 'due_date': when_iso}), kind,
            all_day=True, due_date=when_iso)
        return 'Reminder set: ' + title + ' in Wisp only; synthetic receipt.'
    original = REGISTRY['add_reminder']
    monkeypatch.setitem(REGISTRY, 'add_reminder', Tool('add_reminder', original.description,
        original.parameters, 'assistant_write', synthetic_create))
    async def stream(prompt, sid):
        response = await main.agent({'prompt': prompt, 'session_id': sid, 'debug': False})
        raw = b''
        async for chunk in response.body_iterator:
            raw += chunk if isinstance(chunk, bytes) else chunk.encode()
        return [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith('data: ')]
    sid = sessions.create_session()
    first = (datetime.now() + timedelta(days=3)).date().isoformat()
    second = (datetime.now() + timedelta(days=5)).date().isoformat()
    try:
        events = await stream(f'set all-day reminders on {first} for synthetic alpha '
            f'and another on {second} for synthetic beta', sid)
        assert len(calls) == 2 and events[-1]['type'] == 'done'
        before = reminders.active_between(include_stale=True)
        assert len(before) == 2
        flag = {'model_led':'WISP_MODEL_LED_ROUTING', 'planner':'WISP_PLANNER',
                'shadow':'WISP_TYPED_REMINDERS_SHADOW_ONLY'}.get(mode)
        if flag:
            monkeypatch.setenv(flag, '1')
        route = AsyncMock(side_effect=AssertionError('ambiguous batch reached routing'))
        generation = AsyncMock(side_effect=AssertionError('ambiguous batch reached model'))
        monkeypatch.setattr(main, 'route', route)
        monkeypatch.setattr(main, 'run_agent', generation)
        events = await stream(correction, sid)
        guard = next(e for e in events if e['type'] == 'task_plan')
        assert guard['event'] == 'batch_item_clarification'
        assert guard['trace']['model_invocations'] == 0
        assert 'Which reminder' in ''.join(e.get('text', '') for e in events if e['type'] == 'text')
        assert not [e for e in events if e['type'] == 'tool_call']
        assert len(calls) == 2 and reminders.active_between(include_stale=True) == before
        route.assert_not_awaited()
        generation.assert_not_awaited()
        main.ensure_omlx.assert_not_awaited()
        assert events[-1]['type'] == 'done'
    finally:
        sessions._db.close()
        reminders._db.close()
