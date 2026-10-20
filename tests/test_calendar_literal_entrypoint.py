"""Quoted Calendar identity through compilation and the real SSE entrypoint."""
import json
from unittest.mock import AsyncMock

import pytest


QUOTES = [('"', '"'), ('“', '”'), ("'", "'"), ('`', '`')]
TITLES = ['tmrw', 'calender', 'imsg', 'texxt', 'portfoliio',
          'TMROW; imsg / calender', 'Ordinary title']


@pytest.mark.parametrize('opening,closing', QUOTES)
@pytest.mark.parametrize('title', TITLES)
def test_calendar_identity_is_literal_while_outer_typos_are_normalized(opening, closing, title):
    from service.workflows.reads import compile_read
    prompt = f'Show calender events named {opening}{title}{closing} tmrw'
    assert compile_read(prompt) == ([('get_upcoming', {
        'period': 'tomorrow', 'query': title, 'calendar_only': True})], '')


@pytest.mark.parametrize('prompt', [
    'Show my calender tmrow', 'Check my scedule tmrw',
    'List my schedual tomorrow',
])
def test_unquoted_calendar_typo_controls(prompt):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) == ([('get_upcoming', {'period': 'tomorrow'})], '')


@pytest.mark.asyncio
@pytest.mark.parametrize('opening,closing', QUOTES)
async def test_calendar_stream_executes_exact_quoted_query(tmp_path, monkeypatch, opening, closing):
    from service import main
    from service.memory.store import SessionStore
    from service.assistant.store import AssistantStore
    from service.workflows import reads
    from service.safety.policy import Decision, Tier

    sessions = SessionStore(tmp_path / 'sessions.sqlite')
    assistant = AssistantStore(tmp_path / 'assistant.sqlite')
    monkeypatch.setattr(main, 'store', sessions)
    monkeypatch.setattr(main, 'assistant_store', assistant)
    monkeypatch.delenv('WISP_MODEL_LED_ROUTING', raising=False)
    monkeypatch.setattr(main, 'ensure_omlx', AsyncMock(side_effect=AssertionError('unexpected inference')))
    monkeypatch.setattr(main, 'route', AsyncMock(side_effect=AssertionError('unexpected routing')))
    monkeypatch.setattr(reads, 'decide', lambda *a, **k: Decision(Tier.ALLOW, 'synthetic read'))
    calls = []

    async def synthetic_read(tool, args):
        calls.append((tool.name, args))
        return 'Synthetic matching Calendar event.'

    monkeypatch.setattr(reads, 'run_tool', synthetic_read)
    try:
        response = await main.agent({'prompt': f'Show calender events named {opening}tmrw{closing} tmrow', 'debug': False})
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        events = [json.loads(line[6:]) for line in ''.join(chunks).splitlines() if line.startswith('data: ')]
        assert calls == [('get_upcoming', {'period': 'tomorrow', 'query': 'tmrw', 'calendar_only': True})]
        assert 'Synthetic matching Calendar event.' in ''.join(e.get('text', '') for e in events if e['type'] == 'text')
        assert events[-1]['type'] == 'done'
        main.ensure_omlx.assert_not_awaited()
        main.route.assert_not_awaited()
    finally:
        sessions._db.close()
        assistant._db.close()
