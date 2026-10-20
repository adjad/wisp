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


def test_quoted_time_word_does_not_supply_the_requested_date_range():
    from service.workflows.reads import compile_read
    assert compile_read('Show calendar events named "tmrw"') == ([('get_upcoming', {
        'days': 7, 'query': 'tmrw', 'calendar_only': True})], '')


@pytest.mark.parametrize('prompt', [
    'Explain the phrase "Show calender events named tmrw tomorrow"',
    'Do not show calendar events named "tmrw" tomorrow',
    'Show calendar events named "tmrw" tomorrow and email Mom',
    'Show calendar events named "tmrw" tomorrow from my work account',
    'Show calendar events named "tmrw" tomorrow and summarize my messages',
])
def test_literal_preservation_does_not_broaden_shortcut_admission(prompt):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) is None


@pytest.mark.parametrize('suffix,args', [
    ('; do not browse', {'period': 'tomorrow'}),
    ('; do not use the web', {'period': 'tomorrow'}),
    (', but do not use my notes', {'period': 'tomorrow'}),
    ('; do not use reminders', {'period': 'tomorrow', 'calendar_only': True}),
    ("; don't use Reminders", {'period': 'tomorrow', 'calendar_only': True}),
    ('; do not browse; do not use reminders', {'period': 'tomorrow', 'calendar_only': True}),
    ('; do not use reminders; do not browse', {'period': 'tomorrow', 'calendar_only': True}),
])
def test_explicit_unneeded_source_exclusions_preserve_local_calendar(suffix, args):
    from service.workflows.reads import compile_read
    assert compile_read('Show my calendar tomorrow' + suffix) == ([('get_upcoming', args)], '')


@pytest.mark.parametrize('suffix', [
    '; do not use Calendar', '; do not browse and text Mom',
    '; do not use the web unless needed', '; do not use reminders and email Mom',
])
def test_unknown_or_effectful_source_clause_still_requires_interpretation(suffix):
    from service.workflows.reads import compile_read
    assert compile_read('Show my calendar tomorrow' + suffix) is None


def test_source_denial_inside_a_title_is_only_literal_text():
    from service.workflows.reads import compile_read
    assert compile_read('Show my schedule named "tmrw; do not use Reminders" tomorrow') == (
        [('get_upcoming', {'period': 'tomorrow', 'query': 'tmrw; do not use Reminders'})], '')


@pytest.mark.asyncio
@pytest.mark.parametrize('opening,closing', QUOTES)
@pytest.mark.parametrize('suffix', ['', '; do not browse', '; do not use the web',
                                   ', but do not use my notes', '; do not use reminders'])
async def test_calendar_stream_executes_exact_quoted_query(tmp_path, monkeypatch, opening, closing, suffix):
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
        response = await main.agent({'prompt': f'Show calender events named {opening}tmrw{closing} tmrow{suffix}', 'debug': False})
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
