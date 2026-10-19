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
