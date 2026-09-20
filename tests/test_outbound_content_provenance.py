"""Synthetic regressions for scoped outbound content; never use native apps."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from service.memory.store import SessionStore
from service.safety.policy import Decision, Tier
from service.tools.registry import REGISTRY, Tool
from service.workflows import executor
from service.workflows.compiler import (
    _complete_source_constraint_ledger,
    compile_decision,
    compile_new,
)
from service.workflows.engine import prepare_turn
from service.workflows.models import WorkflowPlan

STALE = 'Calendar: PRIVATE_EVENT\nInbox: OLD_EMAIL\nMessages: PRIVATE_CHAT'


def conversation(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    sid = store.create_session()
    store.add_turn(sid, 'user', 'Daily summary')
    store.add_turn(sid, 'assistant', STALE)
    return store, sid


@pytest.fixture
def delivery(monkeypatch):
    state = SimpleNamespace(reads=[], effects=[], previews=[], allow=False,
                            source='FRESH_EMAIL: Synthetic inbox item')

    async def emit(event):
        pass

    async def confirm(action):
        state.previews.append(action)
        return state.allow

    async def read(**kwargs):
        state.reads.append(kwargs)
        return state.source

    monkeypatch.setattr(executor, 'resolve_destination', lambda *a: ('+15555550123', ''))
    monkeypatch.setattr(executor, 'decide', lambda *a, **k: Decision(Tier.ALLOW, 'synthetic'))
    for name in ('summarize_emails', 'summarize_messages'):
        monkeypatch.setitem(REGISTRY, name, Tool(name, 'synthetic', {
            'properties': {'day': {'type': 'string'}, 'period': {'type': 'string'},
                           'unread': {'type': 'boolean'}, 'account': {'type': 'string'},
                           'conversation': {'type': 'string'}}},
            'assistant_read', read))
    for name in ('send_message', 'draft_message'):
        async def fake_effect(_name=name, **kwargs):
            state.effects.append(kwargs)
            return ('Message draft prepared in Wisp for fixture — nothing has been sent.'
                    if _name == 'draft_message' else 'Message sent to +15555550123.')
        monkeypatch.setitem(REGISTRY, name, Tool(name, 'synthetic', {
            'properties': {'to': {'type': 'string'}, 'text': {'type': 'string'}},
            'required': ['to', 'text']}, 'messages_send', fake_effect))
    state.emit, state.approver = emit, SimpleNamespace(confirm=confirm)
    return state


def execute(state, plan):
    plan.status = 'running'
    with tempfile.TemporaryDirectory(prefix='wisp-effect-claim-') as root:
        store = SessionStore(Path(root) / 'sessions.db')
        try:
            sid = store.create_session()
            store.save_workflow(sid, plan.to_dict())
            return asyncio.run(executor.execute_workflow(
                plan, state.emit, state.approver, store=store, session_id=sid))
        finally:
            store._db.close()


async def agent_events(main, sid, prompt):
    response = await main.agent({'prompt': prompt, 'session_id': sid, 'debug': False})
    events = []
    async for item in response.body_iterator:
        if isinstance(item, bytes):
            item = item.decode()
        events.append(__import__('json').loads(item.removeprefix('data: ').strip()))
    return events


@pytest.mark.parametrize('prompt,args', [
    ('send my emails marked unread to Mom via Messages', {'unread': True}),
])
def test_allowlisted_unread_variants_compile_exactly(prompt, args):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == ['email'] and plan.source_args == {'email': args}


@pytest.mark.parametrize('prompt', [
    'send my emails sent by Alice to Mom via Messages',
    'send my emails I got from Alice to Mom via Messages',
    'send my emails mentioning payroll to Mom via Messages',
    'send my messages mentioning dinner to Mom via email',
    'send my work inbox to Mom via Messages',
    'send my emails from the Work mailbox to Mom via Messages',
    'send my emails marked read to Mom via Messages',
    'send my emails in the Archive folder to Mom via Messages',
    'send my emails with subject payroll to Mom via Messages',
    'send my emails containing payroll to Mom via Messages',
    'send my messages sent by Alice to Mom via email',
    'send my messages I got from Alice to Mom via email',
    'send my messages marked unread to Mom via email',
])
def test_allowlisted_private_source_grammar_rejects_residual_modifiers(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt,source', [
    ('send my emails, but only the ones from Alice, to Mom via Messages', 'email'),
    ('send my emails. Only the ones from Alice. Send them to Mom via Messages', 'email'),
    ('send my emails to Mom via Messages, but only unread ones', 'email'),
    ('send my email summary, filtered to unread, to Mom via Messages', 'email'),
    ('send emails from Alice to Mom via Messages', 'email'),
    ('send the emails from Alice to Mom via Messages', 'email'),
    ('send all emails from Alice to Mom via Messages', 'email'),
    ('send messages from Alice to Mom via email', 'messages'),
    ('send the messages from Alice to Mom via email', 'messages'),
])
def test_complete_private_source_requests_are_claimed_and_fail_closed(prompt, source):
    plan = compile_new(prompt)
    assert plan is not None and source in plan.sources
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt,source', [
    ('send starred emails to Mom via Messages', 'email'),
    ('send important emails to Mom via Messages', 'email'),
    ('send archived emails to Mom via Messages', 'email'),
    ('send Work emails to Mom via Messages', 'email'),
    ('send payroll emails to Mom via Messages', 'email'),
    ('send Alice emails to Mom via Messages', 'email'),
    ('send her emails to Mom via Messages', 'email'),
    ('send unread messages to Mom via email', 'messages'),
    ('send archived messages to Mom via email', 'messages'),
    ('send dinner messages to Mom via email', 'messages'),
    ('send Alice messages to Mom via email', 'messages'),
    ('send her messages to Mom via email', 'messages'),
])
def test_bare_private_source_prefix_modifiers_fail_closed(prompt, source):
    plan = compile_new(prompt)
    assert plan is not None and source in plan.sources
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt', [
    'send my starred and important emails to Mom via Messages',
    'send my unread and important emails to Mom via Messages',
    'send only the unread part of my emails to Mom via Messages',
    'send a summary of the starred items in my emails to Mom via Messages',
    'send the unread portion of my inbox to Mom via Messages',
    'send my "unread" emails to Mom via Messages',
    'send my emails "from Alice" to Mom via Messages',
])
def test_coordinated_and_quoted_email_qualifiers_fail_closed(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == ['email']
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


def test_prior_source_coordination_preserves_bare_unread_email_scope():
    plan = compile_new('send my calendar and unread emails to Mom via Messages')
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == ['calendar', 'email']
    assert plan.source_args == {
        'calendar': {'days': 7},
        'email': {'unread': True},
    }


@pytest.mark.parametrize('prompt', [
    'send my emails and the calendar section to Mom via Messages',
    'send my calendar and the email section to Mom via Messages',
])
def test_coordinated_named_section_preserves_every_independent_source(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == ['calendar', 'email']
    assert plan.source_args == {'calendar': {'days': 7}, 'email': {}}


def test_ambiguous_named_section_never_silently_drops_another_source():
    plan = compile_new(
        'send my emails, the calendar section, to Mom via Messages')
    assert plan is not None and set(plan.sources) == {'calendar', 'email'}
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


NAMED_SECTION_CASES = [
    ('email', noun) for noun in
    ('email', 'emails', 'e-mail', 'e-mails', 'mail', 'inbox')
] + [
    ('messages', noun) for noun in ('message', 'messages', 'text', 'texts')
] + [
    ('calendar', noun) for noun in ('calendar', 'schedule', 'agenda')
] + [
    ('weather', noun) for noun in ('weather', 'forecast')
] + [
    ('reminder', noun) for noun in ('reminder', 'reminders')
] + [
    ('news', noun) for noun in ('news', 'headline', 'headlines')
] + [
    ('stock', noun) for noun in ('stock', 'stocks', 'share', 'shares', 'portfolio')
]


def _named_section_prompt(source, noun, reverse):
    independent = 'my calendar' if source == 'email' else 'my emails'
    section = f'the {noun} section'
    content = f'{section} and {independent}' if reverse else f'{independent} and {section}'
    channel = 'email' if source == 'messages' else 'Messages'
    return f'send {content} to Mom via {channel}'


def _named_section_expected(source, noun):
    sources = (['email', 'calendar'] if source == 'calendar' and noun == 'schedule'
               else ['calendar', 'email'] if source in {'calendar', 'email'}
               else ['email', source])
    args = {
        'calendar': {'days': 7},
        'email': {},
        'messages': {},
        'weather': {'location': ''},
        'reminder': {'query': '', 'scope': 'all'},
        'news': {'query': 'world news today'},
        'stock': {'symbols': []},
    }
    status = {'weather': 'waiting_for_location', 'stock': 'waiting_for_symbols'}.get(
        source, 'ready')
    return sources, {item: args[item] for item in sources}, status


INDEPENDENT_COORDINATION_CASES = [
    ('calendar', 'my calendar'),
    ('email', 'my emails'),
    ('messages', 'my messages'),
    ('reminder', 'my reminders'),
    ('stock', 'my stocks'),
    ('news', 'my news'),
    ('weather', 'my weather'),
]


def _independent_coordination_prompt(source, phrase, reverse):
    section = 'the schedule section' if source == 'reminder' else 'the reminders section'
    content = f'{section} and {phrase}' if reverse else f'{phrase} and {section}'
    channel = 'email' if source == 'messages' else 'Messages'
    return f'send {content} to Mom via {channel}'


def _independent_coordination_expected(source, reverse):
    section_source = 'calendar' if source == 'reminder' else 'reminder'
    if source in {'reminder', 'stock'}:
        sources = ([section_source, source] if reverse
                   else [source, section_source])
    else:
        sources = [source, section_source]
    all_args = {
        'calendar': {'days': 7},
        'email': {},
        'messages': {},
        'reminder': {'query': '', 'scope': 'all'},
        'stock': {'symbols': []},
        'news': {'query': 'world news today'},
        'weather': {'location': ''},
    }
    status = {'stock': 'waiting_for_symbols', 'weather': 'waiting_for_location'}.get(
        source, 'ready')
    return sources, {item: all_args[item] for item in sources}, status


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('source,phrase', INDEPENDENT_COORDINATION_CASES)
def test_coordinated_independent_sources_survive_named_section_union(
        source, phrase, reverse):
    prompt = _independent_coordination_prompt(source, phrase, reverse)
    plan = compile_new(prompt)
    sources, args, status = _independent_coordination_expected(source, reverse)
    assert plan is not None and plan.sources == sources
    assert plan.source_args == args and plan.status == status


@pytest.mark.parametrize('prompt,status', [
    ('send my reminders summary to Mom via Messages', 'ready'),
    ('send my urgent reminders summary to Mom via Messages', 'waiting_for_content'),
])
def test_reminders_summary_structural_noun_and_modifier_boundary(prompt, status):
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == ['reminder']
    assert plan.status == status
    if status == 'ready':
        assert plan.source_args == {'reminder': {'query': '', 'scope': 'all'}}
    else:
        assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('source,noun', NAMED_SECTION_CASES)
def test_every_named_section_noun_preserves_ordered_source_union(source, noun, reverse):
    plan = compile_new(_named_section_prompt(source, noun, reverse))
    sources, args, status = _named_section_expected(source, noun)
    assert plan is not None and plan.sources == sources
    assert plan.source_args == args and plan.status == status


@pytest.mark.parametrize('prompt,source,args', [
    ('send emails to Mom via Messages', 'email', {}),
    ('send unread emails to Mom via Messages', 'email', {'unread': True}),
    ('send messages to Mom via email', 'messages', {}),
])
def test_neutral_bare_private_sources_keep_exact_supported_args(prompt, source, args):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == [source] and plan.source_args == {source: args}


@pytest.mark.parametrize('prompt,args', [
    ('send my emails marked unread to Mom via Messages', {'unread': True}),
    ('send emails to Mom via Messages', {}),
    ('send unread emails to Mom via Messages', {'unread': True}),
    ('send messages to Mom via email', {}),
    ("send my emails that I haven't read to Mom via Messages", {'unread': True}),
    ('send my messages in the Family Chat conversation to Mom via email',
     {'conversation': 'Family Chat'}),
    ('send my emails sent by Alice to Mom via Messages', None),
    ('send my emails I got from Alice to Mom via Messages', None),
    ('send my emails mentioning payroll to Mom via Messages', None),
    ('send my messages mentioning dinner to Mom via email', None),
    ('send my work inbox to Mom via Messages', None),
    ('send my emails from the Work mailbox to Mom via Messages', None),
    ('send my emails marked read to Mom via Messages', None),
    ('send my emails in the Archive folder to Mom via Messages', None),
    ('send my emails with subject payroll to Mom via Messages', None),
    ('send my messages sent by Alice to Mom via email', None),
    ('send my emails, but only the ones from Alice, to Mom via Messages', None),
    ('send my emails. Only the ones from Alice. Send them to Mom via Messages', None),
    ('send my emails to Mom via Messages, but only unread ones', None),
    ('send my email summary, filtered to unread, to Mom via Messages', None),
    ('send emails from Alice to Mom via Messages', None),
    ('send the emails from Alice to Mom via Messages', None),
    ('send all emails from Alice to Mom via Messages', None),
    ('send messages from Alice to Mom via email', None),
    ('send the messages from Alice to Mom via email', None),
    ('send starred emails to Mom via Messages', None),
    ('send important emails to Mom via Messages', None),
    ('send archived emails to Mom via Messages', None),
    ('send Work emails to Mom via Messages', None),
    ('send payroll emails to Mom via Messages', None),
    ('send Alice emails to Mom via Messages', None),
    ('send her emails to Mom via Messages', None),
    ('send unread messages to Mom via email', None),
    ('send archived messages to Mom via email', None),
    ('send dinner messages to Mom via email', None),
    ('send Alice messages to Mom via email', None),
    ('send her messages to Mom via email', None),
    ('send my starred and important emails to Mom via Messages', None),
    ('send my unread and important emails to Mom via Messages', None),
    ('send only the unread part of my emails to Mom via Messages', None),
    ('send a summary of the starred items in my emails to Mom via Messages', None),
    ('send the unread portion of my inbox to Mom via Messages', None),
    ('send my "unread" emails to Mom via Messages', None),
    ('send my emails "from Alice" to Mom via Messages', None),
])
def test_allowlisted_private_source_grammar_at_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt, args):
    from service import main
    from service.memory import context
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []
    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Private source phrase escaped the workflow boundary')
    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    asyncio.run(agent_events(main, sid, prompt))
    if args is None:
        assert not delivery.reads and not delivery.previews and not delivery.effects
        assert not fallbacks
        assert store.latest_workflow(sid)['status'] == 'waiting_for_content'
    else:
        assert delivery.reads == [args]
        assert len(delivery.previews) == 1 and not delivery.effects
    store._db.close()


def test_calendar_coordination_keeps_unread_email_scope_at_agent_boundary(
        tmp_path, monkeypatch, delivery):
    from service import main
    from service.memory import context

    async def calendar_read(**kwargs):
        delivery.reads.append(kwargs)
        return 'CALENDAR: Synthetic event'

    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {'days': {'type': 'integer'}}},
        'calendar_read', calendar_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Coordinated private scope escaped the workflow boundary')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    asyncio.run(agent_events(
        main, sid, 'send my calendar and unread emails to Mom via Messages'))

    assert delivery.reads == [{'days': 7}, {'unread': True}]
    assert not fallbacks and len(delivery.previews) == 1 and not delivery.effects
    store._db.close()


@pytest.mark.parametrize('prompt', [
    'send my emails and the calendar section to Mom via Messages',
    'send my calendar and the email section to Mom via Messages',
])
def test_coordinated_named_section_reads_every_source_at_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt):
    from service import main
    from service.memory import context

    async def calendar_read(**kwargs):
        delivery.reads.append(kwargs)
        return 'CALENDAR: Synthetic event'

    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {'days': {'type': 'integer'}}},
        'calendar_read', calendar_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Coordinated source sections escaped the workflow boundary')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    asyncio.run(agent_events(main, sid, prompt))

    assert delivery.reads == [{'days': 7}, {}]
    assert not fallbacks and len(delivery.previews) == 1 and not delivery.effects
    store._db.close()


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('source,noun', NAMED_SECTION_CASES)
def test_every_named_section_noun_reaches_exact_agent_boundary(
        tmp_path, monkeypatch, delivery, source, noun, reverse):
    from service import main
    from service.memory import context
    calls = []

    def reader(name):
        async def read(**kwargs):
            calls.append((name, kwargs))
            return f'{name}: Synthetic source result'
        return read

    schemas = {
        'get_upcoming': {'properties': {'days': {'type': 'integer'}}},
        'summarize_emails': {'properties': {}},
        'summarize_messages': {'properties': {}},
        'search_reminders': {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'web_search': {'properties': {'query': {'type': 'string'}}},
        'get_weather': {'properties': {
            'location': {'type': 'string'}, 'period': {'type': 'string'}}},
        'get_stock_price': {'properties': {
            'symbols': {'type': 'array'}, 'period': {'type': 'string'}}},
    }
    source_tools = {
        'calendar': 'get_upcoming',
        'email': 'summarize_emails',
        'messages': 'summarize_messages',
        'reminder': 'search_reminders',
        'news': 'web_search',
        'weather': 'get_weather',
        'stock': 'get_stock_price',
    }
    for tool_name, schema in schemas.items():
        monkeypatch.setitem(REGISTRY, tool_name, Tool(
            tool_name, 'synthetic', schema, 'assistant_read', reader(tool_name)))

    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Named source section escaped the workflow boundary')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    prompt = _named_section_prompt(source, noun, reverse)
    events = asyncio.run(agent_events(main, sid, prompt))
    sources, args, status = _named_section_expected(source, noun)

    assert not fallbacks and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    if status == 'ready':
        assert calls == [(source_tools[item], args[item]) for item in sources]
        assert len(delivery.previews) == 1
    else:
        assert not calls and not delivery.previews
        persisted = store.latest_workflow(sid)
        assert persisted is not None and persisted['status'] == status
    store._db.close()


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('source,phrase', INDEPENDENT_COORDINATION_CASES)
def test_coordinated_independent_sources_reach_exact_agent_boundary(
        tmp_path, monkeypatch, delivery, source, phrase, reverse):
    from service import main
    from service.memory import context
    calls = []

    def reader(name):
        async def read(**kwargs):
            calls.append((name, kwargs))
            return f'{name}: Synthetic source result'
        return read

    schemas = {
        'get_upcoming': {'properties': {'days': {'type': 'integer'}}},
        'summarize_emails': {'properties': {}},
        'summarize_messages': {'properties': {}},
        'search_reminders': {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'web_search': {'properties': {'query': {'type': 'string'}}},
        'get_weather': {'properties': {'location': {'type': 'string'}}},
        'get_stock_price': {'properties': {'symbols': {'type': 'array'}}},
    }
    source_tools = {
        'calendar': 'get_upcoming',
        'email': 'summarize_emails',
        'messages': 'summarize_messages',
        'reminder': 'search_reminders',
        'stock': 'get_stock_price',
        'news': 'web_search',
        'weather': 'get_weather',
    }
    for tool_name, schema in schemas.items():
        monkeypatch.setitem(REGISTRY, tool_name, Tool(
            tool_name, 'synthetic', schema, 'assistant_read', reader(tool_name)))

    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Coordinated independent source escaped the workflow boundary')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    prompt = _independent_coordination_prompt(source, phrase, reverse)
    events = asyncio.run(agent_events(main, sid, prompt))
    sources, args, status = _independent_coordination_expected(source, reverse)

    assert not fallbacks and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    if status == 'ready':
        assert calls == [(source_tools[item], args[item]) for item in sources]
        assert len(delivery.previews) == 1
    else:
        assert not calls and not delivery.previews
        persisted = store.latest_workflow(sid)
        assert persisted is not None and persisted['status'] == status
    store._db.close()


@pytest.mark.parametrize('prompt,ready', [
    ('send my reminders summary to Mom via Messages', True),
    ('send my urgent reminders summary to Mom via Messages', False),
])
def test_reminders_summary_reaches_or_stops_at_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt, ready):
    from service import main
    from service.memory import context
    calls = []

    async def read(**kwargs):
        calls.append(kwargs)
        return 'REMINDERS: Synthetic source result'

    monkeypatch.setitem(REGISTRY, 'search_reminders', Tool(
        'search_reminders', 'synthetic', {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'assistant_read', read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Reminder summary escaped the workflow boundary')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not fallbacks and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    if ready:
        assert calls == [{'query': '', 'scope': 'all'}]
        assert len(delivery.previews) == 1
    else:
        assert not calls and not delivery.previews
        persisted = store.latest_workflow(sid)
        assert persisted is not None and persisted['status'] == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('prompt,scope', [
    ('send my reminders due today and the calendar section to Mom via Messages',
     'today'),
    ('send today\'s reminders and the calendar section to Mom via Messages',
     'today'),
    ('send my reminders for tomorrow and the calendar section to Mom via Messages',
     'tomorrow'),
])
def test_reminder_qualifiers_compile_only_to_enforceable_scope(prompt, scope):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert set(plan.sources) == {'reminder', 'calendar'}
    assert plan.source_args['reminder'] == {'query': '', 'scope': scope}


@pytest.mark.parametrize('qualifier', [
    'urgent reminders',
    'overdue reminders',
    'incomplete reminders',
    'reminders from Work',
    'upcoming reminders',
    'past due reminders',
])
def test_unsupported_reminder_qualifiers_fail_closed(qualifier):
    plan = compile_new(
        f'send my {qualifier} and the calendar section to Mom via Messages')
    assert plan is not None and 'reminder' in plan.sources
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('content', [
    'my stocks, the reminders section',
    'the reminders section, my stocks',
])
def test_comma_source_mixtures_are_claimed_but_require_relationship(content):
    plan = compile_new(f'send {content} to Mom via Messages')
    assert plan is not None and set(plan.sources) == {'stock', 'reminder'}
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt,sources,status', [
    ('share with Mom the calendar section via Messages', ['calendar'], 'ready'),
    ('send my shares and the calendar section to Mom via Messages',
     ['calendar', 'stock'], 'waiting_for_symbols'),
    ('send Mom a message with my calendar via Messages', ['calendar'], 'ready'),
    ('send my messages and the calendar section to Mom via email',
     ['calendar', 'messages'], 'ready'),
    ('email Mom "I will arrive at six"', [], None),
])
def test_source_lexical_roles_exclude_effect_and_channel_nouns(
        prompt, sources, status):
    plan = compile_new(prompt)
    if status is None:
        assert plan is None
    else:
        assert plan is not None and plan.sources == sources
        assert plan.status == status


@pytest.mark.parametrize('prompt', [
    'send my email and the calendar section to Mom via Messages',
    'send the calendar section and my email to Mom via Messages',
])
def test_determiner_owned_singular_email_coordinates_as_payload(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == ['calendar', 'email']
    assert plan.source_args == {'calendar': {'days': 7}, 'email': {}}


@pytest.mark.parametrize('prompt,expected_calls,ready', [
    ('send my reminders due today and the calendar section to Mom via Messages',
     [('get_upcoming', {'days': 7}),
      ('search_reminders', {'query': '', 'scope': 'today'})], True),
    ('send my urgent reminders and the calendar section to Mom via Messages', [], False),
    ('send my overdue reminders and the calendar section to Mom via Messages', [], False),
    ('send my incomplete reminders and the calendar section to Mom via Messages', [], False),
    ('send my reminders from Work and the calendar section to Mom via Messages', [], False),
    ('send my stocks, the reminders section to Mom via Messages', [], False),
    ('send the reminders section, my stocks to Mom via Messages', [], False),
    ('share with Mom the calendar section via Messages',
     [('get_upcoming', {'days': 7})], True),
    ('send my email and the calendar section to Mom via Messages',
     [('get_upcoming', {'days': 7}), ('summarize_emails', {})], True),
    ('send the calendar section and my email to Mom via Messages',
     [('get_upcoming', {'days': 7}), ('summarize_emails', {})], True),
])
def test_source_role_and_reminder_scope_at_real_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt, expected_calls, ready):
    from service import main
    from service.memory import context
    calls = []
    sentinel = 'UNREQUESTED_REMINDER_SENTINEL'

    def reader(name):
        async def read(**kwargs):
            calls.append((name, kwargs))
            if name == 'search_reminders':
                return (sentinel if kwargs.get('scope') == 'all'
                        else 'TODAY_REMINDER: Synthetic scoped item')
            return f'{name}: Synthetic source result'
        return read

    schemas = {
        'get_upcoming': {'properties': {
            'days': {'type': 'integer'}, 'period': {'type': 'string'}}},
        'summarize_emails': {'properties': {}},
        'search_reminders': {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'get_stock_price': {'properties': {'symbols': {'type': 'array'}}},
    }
    for name, schema in schemas.items():
        monkeypatch.setitem(REGISTRY, name, Tool(
            name, 'synthetic', schema, 'assistant_read', reader(name)))

    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Source-role request escaped the workflow boundary')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not fallbacks and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    assert calls == expected_calls
    if ready:
        assert len(delivery.previews) == 1
        assert sentinel not in delivery.previews[0]['args']['text']
    else:
        assert not delivery.previews
        persisted = store.latest_workflow(sid)
        assert persisted is not None
        assert persisted['status'] in {'waiting_for_content', 'waiting_for_symbols'}
    store._db.close()


def test_trailing_reminder_restriction_refines_earlier_clause():
    plan = compile_new(
        'send my reminders and the calendar section to Mom via Messages, '
        'but only reminders due today')
    assert plan is not None and plan.status == 'ready'
    assert set(plan.sources) == {'reminder', 'calendar'}
    assert plan.source_args['reminder'] == {'query': '', 'scope': 'today'}


@pytest.mark.parametrize('prompt,scope', [
    ('send my reminders "due today" to Mom via Messages', 'today'),
    ('send my reminders "tomorrow" to Mom via Messages', 'tomorrow'),
])
def test_quoted_reminder_day_scope_is_preserved(prompt, scope):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == ['reminder']
    assert plan.source_args == {'reminder': {'query': '', 'scope': scope}}


@pytest.mark.parametrize('prompt', [
    'send my "work" reminders to Mom via Messages',
    'send my "incomplete" reminders to Mom via Messages',
    'send my reminders "overdue" to Mom via Messages',
])
def test_quoted_unsupported_reminder_filters_fail_closed(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == ['reminder']
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt', [
    'send my reminders due today and my reminders due tomorrow to Mom via Messages',
    'send my reminders and my reminders due tomorrow to Mom via Messages',
])
def test_repeated_reminder_clauses_require_clarification(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == ['reminder']
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt,expected_scope,ready', [
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only reminders due today', 'today', True),
    ('send my reminders "due today" to Mom via Messages', 'today', True),
    ('send my reminders "tomorrow" to Mom via Messages', 'tomorrow', True),
    ('send my "work" reminders to Mom via Messages', None, False),
    ('send my "incomplete" reminders to Mom via Messages', None, False),
    ('send my reminders "overdue" to Mom via Messages', None, False),
    ('send my reminders due today and my reminders due tomorrow to Mom via Messages',
     None, False),
    ('send my reminders and my reminders due tomorrow to Mom via Messages',
     None, False),
])
def test_full_reminder_clause_scope_at_real_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt, expected_scope, ready):
    from service import main
    from service.memory import context
    calls = []
    sentinel = 'UNREQUESTED_FUTURE_REMINDER'

    async def reminder_read(**kwargs):
        calls.append(('search_reminders', kwargs))
        return (sentinel if kwargs.get('scope') == 'all'
                else 'SCOPED_REMINDER: Synthetic matching item')

    async def calendar_read(**kwargs):
        calls.append(('get_upcoming', kwargs))
        return 'CALENDAR: Synthetic event'

    monkeypatch.setitem(REGISTRY, 'search_reminders', Tool(
        'search_reminders', 'synthetic', {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'assistant_read', reminder_read))
    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {
            'days': {'type': 'integer'}, 'period': {'type': 'string'}}},
        'calendar_read', calendar_read))

    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Reminder scope escaped the workflow boundary')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not fallbacks and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    if ready:
        reminder_calls = [args for name, args in calls if name == 'search_reminders']
        assert reminder_calls == [{'query': '', 'scope': expected_scope}]
        assert len(delivery.previews) == 1
        assert sentinel not in delivery.previews[0]['args']['text']
    else:
        assert not calls and not delivery.previews
        persisted = store.latest_workflow(sid)
        assert persisted is not None and persisted['status'] == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('tail,scope', [
    ('but only the ones due today', 'today'),
    ('but only those due today', 'today'),
    ('but only items due today', 'today'),
    ('but only due today', 'today'),
    ('but only "due today"', 'today'),
    ('only the ones due tomorrow', 'tomorrow'),
])
def test_sentence_level_reminder_restrictions_bind_exact_scope(tail, scope):
    punctuation = ', ' if tail.startswith('but') else ' '
    plan = compile_new(
        'send my reminders and the calendar section to Mom via Messages'
        f'{punctuation}{tail}')
    assert plan is not None and plan.status == 'ready'
    assert set(plan.sources) == {'reminder', 'calendar'}
    assert plan.source_args['reminder'] == {'query': '', 'scope': scope}


@pytest.mark.parametrize('tail', [
    'but only the ones from Work',
    'but only "from Work"',
    'but only "incomplete"',
    'but only today',
    "but only today's",
    "but only tomorrow's",
])
def test_unsupported_or_ambiguous_sentence_reminder_restrictions_fail_closed(tail):
    plan = compile_new(
        'send my reminders and the calendar section to Mom via Messages, '
        f'{tail}')
    assert plan is not None and 'reminder' in plan.sources
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt', [
    ('send the calendar section and my reminders to Mom via Messages, '
     'but only the ones due today'),
    ('send the calendar section and my reminders to Mom via Messages '
     'only the ones due today'),
])
def test_sentence_reminder_restriction_supports_source_order_and_punctuation(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.source_args['reminder'] == {'query': '', 'scope': 'today'}


def test_explicit_calendar_tail_does_not_narrow_reminders():
    plan = compile_new(
        'send my reminders and the calendar section to Mom via Messages, '
        'but only calendar events today')
    assert plan is not None and plan.status == 'ready'
    assert plan.source_args['reminder'] == {'query': '', 'scope': 'all'}
    assert plan.source_args['calendar'] == {'period': 'today'}


def test_calendar_only_tail_and_positive_reminder_query_remain_supported():
    calendar = compile_new(
        'send the calendar section to Mom via Messages, but only today')
    assert calendar is not None and calendar.status == 'ready'
    assert calendar.sources == ['calendar']
    assert calendar.source_args == {'calendar': {'period': 'today'}}

    query = compile_new(
        'draft an imessage to send to mom with my vaccine information '
        'from reminders about when it is')
    assert query is not None and query.status == 'ready'
    assert query.source_args == {'reminder': {'query': 'vaccine', 'scope': 'all'}}


@pytest.mark.parametrize('prompt,scope,ready', [
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only the ones due today', 'today', True),
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only those due today', 'today', True),
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only items due today', 'today', True),
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only due today', 'today', True),
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only "due today"', 'today', True),
    ('send the calendar section and my reminders to Mom via Messages '
     'only the ones due tomorrow', 'tomorrow', True),
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only the ones from Work', None, False),
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only "from Work"', None, False),
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only today', None, False),
    ('send my reminders and the calendar section to Mom via Messages, '
     "but only today's", None, False),
    ('send my reminders and the calendar section to Mom via Messages, '
     "but only tomorrow's", None, False),
])
def test_sentence_reminder_restrictions_at_real_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt, scope, ready):
    from service import main
    from service.memory import context
    calls = []
    sentinels = {'UNREQUESTED_FUTURE_REMINDER', 'UNREQUESTED_PERSONAL_REMINDER'}

    async def reminder_read(**kwargs):
        calls.append(('search_reminders', kwargs))
        if kwargs.get('scope') == 'all' and not kwargs.get('query'):
            return '\n'.join(sorted(sentinels))
        return 'SCOPED_REMINDER: Synthetic matching item'

    async def calendar_read(**kwargs):
        calls.append(('get_upcoming', kwargs))
        return 'CALENDAR: Synthetic event'

    monkeypatch.setitem(REGISTRY, 'search_reminders', Tool(
        'search_reminders', 'synthetic', {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'assistant_read', reminder_read))
    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {
            'days': {'type': 'integer'}, 'period': {'type': 'string'}}},
        'calendar_read', calendar_read))

    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Trailing reminder restriction escaped workflow ownership')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not fallbacks and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    if ready:
        reminder_calls = [args for name, args in calls if name == 'search_reminders']
        assert reminder_calls == [{'query': '', 'scope': scope}]
        assert len(delivery.previews) == 1
        body = delivery.previews[0]['args']['text']
        assert not any(sentinel in body for sentinel in sentinels)
    else:
        assert not calls and not delivery.previews
        persisted = store.latest_workflow(sid)
        assert persisted is not None and persisted['status'] == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('prompt,reminder_scope,calendar_scope', [
    ('send my reminders and the calendar section to Mom via Messages, '
     'just the ones due today', 'today', None),
    ('send my reminders and the calendar section to Mom via Messages, '
     'only due today and only calendar tomorrow', 'today', 'tomorrow'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'only calendar tomorrow and only due today', 'today', 'tomorrow'),
    ('send the calendar section and my reminders to Mom via Messages '
     'just the ones due tomorrow and only calendar today', 'tomorrow', 'today'),
])
def test_constraint_ledger_enforces_every_source_owned_limiter(
        prompt, reminder_scope, calendar_scope):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.source_args['reminder'] == {
        'query': '', 'scope': reminder_scope}
    assert plan.source_args['calendar'] == (
        {'period': calendar_scope} if calendar_scope else {'days': 7})


@pytest.mark.parametrize('prompt', [
    ('send my reminders and the calendar section to Mom via Messages '
     '"from Work"'),
    ('send my reminders and the calendar section "work" to Mom via Messages'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only the incomplete ones and only the ones due today'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'only the ones due today and just the incomplete ones'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'just the ones from Work and only calendar tomorrow'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'only due today and only calendar tomorrow and only today'),
])
def test_constraint_ledger_rejects_any_unconsumed_or_unsupported_span(prompt):
    plan = compile_new(prompt)
    assert plan is not None and 'reminder' in plan.sources
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt', [
    'send my reminders and the calendar section to Mom via Messages',
    'just send my reminders and the calendar section to Mom via Messages',
    'please send my reminders and the calendar section to Mom via Messages now',
])
def test_constraint_ledger_leaves_unrestricted_prose_unchanged(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.source_args == {
        'calendar': {'days': 7},
        'reminder': {'query': '', 'scope': 'all'},
    }


def test_calendar_only_constraint_remains_calendar_owned():
    plan = compile_new(
        'send the calendar section to Mom via Messages, only calendar tomorrow')
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == ['calendar']
    assert plan.source_args == {'calendar': {'period': 'tomorrow'}}


@pytest.mark.parametrize('prompt,reminder_scope,calendar_scope,ready', [
    ('send my reminders and the calendar section to Mom via Messages, '
     'just the ones due today', 'today', None, True),
    ('send my reminders and the calendar section to Mom via Messages, '
     'only due today and only calendar tomorrow', 'today', 'tomorrow', True),
    ('send my reminders and the calendar section to Mom via Messages, '
     'only calendar tomorrow and only due today', 'today', 'tomorrow', True),
    ('send the calendar section and my reminders to Mom via Messages '
     'just the ones due tomorrow and only calendar today', 'tomorrow', 'today', True),
    ('send my reminders and the calendar section to Mom via Messages '
     '"from Work"', None, None, False),
    ('send my reminders and the calendar section "work" to Mom via Messages',
     None, None, False),
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only the incomplete ones and only the ones due today', None, None, False),
    ('send my reminders and the calendar section to Mom via Messages, '
     'only the ones due today and just the incomplete ones', None, None, False),
    ('send my reminders and the calendar section to Mom via Messages, '
     'just the ones from Work and only calendar tomorrow', None, None, False),
])
def test_constraint_ledger_at_real_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt,
        reminder_scope, calendar_scope, ready):
    from service import main
    from service.memory import context
    calls = []
    sentinels = {
        'UNREQUESTED_FUTURE_REMINDER',
        'UNREQUESTED_PERSONAL_REMINDER',
        'UNREQUESTED_CALENDAR_DAY',
    }

    async def reminder_read(**kwargs):
        calls.append(('search_reminders', kwargs))
        if kwargs.get('scope') == 'all':
            return 'UNREQUESTED_FUTURE_REMINDER\nUNREQUESTED_PERSONAL_REMINDER'
        return 'SCOPED_REMINDER: Synthetic matching item'

    async def calendar_read(**kwargs):
        calls.append(('get_upcoming', kwargs))
        if calendar_scope and kwargs.get('period') != calendar_scope:
            return 'UNREQUESTED_CALENDAR_DAY'
        return 'SCOPED_CALENDAR: Synthetic matching event'

    monkeypatch.setitem(REGISTRY, 'search_reminders', Tool(
        'search_reminders', 'synthetic', {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'assistant_read', reminder_read))
    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {
            'days': {'type': 'integer'}, 'period': {'type': 'string'}}},
        'calendar_read', calendar_read))

    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Constraint ledger escaped workflow ownership')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not fallbacks and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    if ready:
        assert ('search_reminders', {
            'query': '', 'scope': reminder_scope}) in calls
        expected_calendar = ({'period': calendar_scope}
                             if calendar_scope else {'days': 7})
        assert ('get_upcoming', expected_calendar) in calls
        assert len(delivery.previews) == 1
        body = delivery.previews[0]['args']['text']
        assert not any(sentinel in body for sentinel in sentinels)
    else:
        assert not calls and not delivery.previews
        persisted = store.latest_workflow(sid)
        assert persisted is not None and persisted['status'] == 'waiting_for_content'
    store._db.close()


def test_constraint_ledger_exposes_complete_consumption_and_reflection_inputs():
    ledger = _complete_source_constraint_ledger(
        'send my reminders and the calendar section to Mom via Messages, '
        'only due today and only calendar tomorrow')
    assert ledger.complete
    assert ledger.candidate_count == ledger.consumed_count == 2
    assert ledger.scopes == {'reminder': 'today', 'calendar': 'tomorrow'}
    assert ledger.selected_sources is None and not ledger.residue

    selection = _complete_source_constraint_ledger(
        'send my reminders and the calendar section to Mom via Messages, '
        'but only the calendar section')
    assert selection.complete
    assert selection.candidate_count == selection.consumed_count == 1
    assert selection.selected_sources == frozenset({'calendar'})


@pytest.mark.parametrize('prompt,scope', [
    ('send my reminders and the calendar section to Mom via Messages, '
     'limited to those due today', 'today'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'restricted to the ones due tomorrow', 'tomorrow'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'specifically items due today', 'today'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'exclusively due tomorrow', 'tomorrow'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'solely the ones due today', 'today'),
])
def test_constraint_introducers_bind_reminder_scope(prompt, scope):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.source_args['reminder'] == {'query': '', 'scope': scope}


@pytest.mark.parametrize('residue', [
    'from Work',
    'incomplete',
    'urgent',
    '"from Work"',
    '"incomplete"',
])
def test_earlier_residue_cannot_be_licensed_by_later_supported_limiter(residue):
    plan = compile_new(
        'send my reminders and the calendar section to Mom via Messages, '
        f'{residue}, only the ones due today')
    assert plan is not None and plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


SOURCE_SELECTION_CONSTRAINT_CASES = [
    ('calendar',
     'send my reminders and the calendar section to Mom via Messages, '
     'but only the calendar section',
     {'days': 7}, 'ready'),
    ('reminder',
     'send the calendar section and my reminders to Mom via Messages, '
     'but only reminders',
     {'query': '', 'scope': 'all'}, 'ready'),
    ('email',
     'send my emails and the calendar section to Mom via Messages, '
     'but only the email section',
     {}, 'ready'),
    ('messages',
     'send my messages and the calendar section to Mom via email, '
     'but only the messages section',
     {}, 'ready'),
    ('news',
     'send my news and the reminders section to Mom via Messages, '
     'but only news',
     {'query': 'world news today'}, 'ready'),
    ('stock',
     'send AAPL stocks and the reminders section to Mom via Messages, '
     'but only stocks',
     {'symbols': ['AAPL']}, 'ready'),
    ('weather',
     'send my emails and the weather section to Mom via Messages, '
     'but only weather',
     {'location': ''}, 'waiting_for_location'),
]


@pytest.mark.parametrize('source,prompt,args,status', SOURCE_SELECTION_CONSTRAINT_CASES)
def test_source_selection_constraint_is_reflected_in_read_plan(
        source, prompt, args, status):
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == [source]
    assert plan.source_args == {source: args}
    assert plan.status == status


@pytest.mark.parametrize('prompt', [
    ('send my reminders and the calendar section to Mom via Messages, '
     'but only the calendar section'),
    ('send the calendar section and my reminders to Mom via Messages, '
     'but only calendar'),
])
def test_only_calendar_selection_never_retains_reminder_read(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == ['calendar']
    assert plan.source_args == {'calendar': {'days': 7}}


@pytest.mark.parametrize('source,prompt,args,status', SOURCE_SELECTION_CONSTRAINT_CASES)
def test_source_selection_constraint_at_real_agent_boundary(
        tmp_path, monkeypatch, delivery, source, prompt, args, status):
    from service import main
    from service.memory import context
    calls = []

    def reader(name):
        async def read(**kwargs):
            calls.append((name, kwargs))
            return f'{name}: SELECTED_SOURCE_RESULT'
        return read

    schemas = {
        'get_upcoming': {'properties': {'days': {'type': 'integer'}}},
        'search_reminders': {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'summarize_emails': {'properties': {}},
        'summarize_messages': {'properties': {}},
        'web_search': {'properties': {'query': {'type': 'string'}}},
        'get_stock_price': {'properties': {'symbols': {'type': 'array'}}},
        'get_weather': {'properties': {'location': {'type': 'string'}}},
    }
    tools = {
        'calendar': 'get_upcoming',
        'reminder': 'search_reminders',
        'email': 'summarize_emails',
        'messages': 'summarize_messages',
        'news': 'web_search',
        'stock': 'get_stock_price',
        'weather': 'get_weather',
    }
    for name, schema in schemas.items():
        monkeypatch.setitem(REGISTRY, name, Tool(
            name, 'synthetic', schema, 'assistant_read', reader(name)))

    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Source selection escaped workflow ownership')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not fallbacks and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    if status == 'ready':
        assert calls == [(tools[source], args)]
        assert len(delivery.previews) == 1
        assert 'PRIVATE_REMINDER_SENTINEL' not in ' '.join(
            str(value) for value in delivery.previews[0]['args'].values())
    else:
        assert not calls and not delivery.previews
        persisted = store.latest_workflow(sid)
        assert persisted is not None and persisted['status'] == status
    store._db.close()


@pytest.mark.parametrize('prompt,scope', [
    ('send my reminders and the calendar section to Mom via Messages, '
     'limited to those due today', 'today'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'restricted to those due tomorrow', 'tomorrow'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'specifically the ones due today', 'today'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'exclusively the ones due tomorrow', 'tomorrow'),
    ('send my reminders and the calendar section to Mom via Messages, '
     'solely items due today', 'today'),
])
def test_constraint_introducers_at_real_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt, scope):
    from service import main
    from service.memory import context
    calls = []

    async def reminder_read(**kwargs):
        calls.append(kwargs)
        return ('PRIVATE_REMINDER_SENTINEL' if kwargs.get('scope') == 'all'
                else 'SCOPED_REMINDER_RESULT')

    async def calendar_read(**kwargs):
        return 'CALENDAR_RESULT'

    monkeypatch.setitem(REGISTRY, 'search_reminders', Tool(
        'search_reminders', 'synthetic', {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'assistant_read', reminder_read))
    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {
            'days': {'type': 'integer'}, 'period': {'type': 'string'}}},
        'calendar_read', calendar_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Restriction introducer escaped workflow ownership')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))
    assert not fallbacks and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    assert calls == [{'query': '', 'scope': scope}]
    assert len(delivery.previews) == 1
    assert 'PRIVATE_REMINDER_SENTINEL' not in delivery.previews[0]['args']['text']
    store._db.close()


@pytest.mark.parametrize('residue', ['from Work', 'incomplete', 'urgent'])
def test_unsupported_residue_before_supported_limiter_stops_real_agent(
        tmp_path, monkeypatch, delivery, residue):
    from service import main
    from service.memory import context
    calls = []

    async def read(**kwargs):
        calls.append(kwargs)
        return 'PRIVATE_REMINDER_SENTINEL'

    monkeypatch.setitem(REGISTRY, 'search_reminders', Tool(
        'search_reminders', 'synthetic', {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'assistant_read', read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Unsupported residue escaped workflow ownership')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    prompt = ('send my reminders and the calendar section to Mom via Messages, '
              f'{residue}, only the ones due today')
    events = asyncio.run(agent_events(main, sid, prompt))
    assert not fallbacks and not delivery.effects and not calls and not delivery.previews
    assert not [event for event in events if event.get('type') == 'error']
    persisted = store.latest_workflow(sid)
    assert persisted is not None and persisted['status'] == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('prompt', [
    'what is on my email and can you send it to mom',
    'send my email summary to mom',
])
def test_reported_sequences_read_email_before_confirmation(tmp_path, delivery, prompt):
    store, sid = conversation(tmp_path)
    first = prepare_turn(store, sid, prompt)
    assert first.plan.sources == ['email'] and first.plan.artifact_text == ''
    assert first.plan.recipient.lower() == 'mom'
    assert first.plan.status == 'waiting_for_channel'
    store.add_turn(sid, 'user', prompt)
    store.add_turn(sid, 'assistant', first.response)
    second = prepare_turn(store, sid, 'messages')
    assert second.decision.direct_calls[0] == ('summarize_emails', {})
    result = execute(delivery, second.plan)
    assert result.status == 'denied'
    assert [c['name'] for c in result.tool_calls] == ['summarize_emails', 'send_message']
    assert len(delivery.previews) == 1 and delivery.effects == []
    body = delivery.previews[0]['args']['text']
    assert 'FRESH_EMAIL' in body
    assert all(marker not in body for marker in ('PRIVATE_EVENT', 'OLD_EMAIL', 'PRIVATE_CHAT'))


@pytest.mark.parametrize('prompt,source,args', [
    ('send only the email section to Mom via Messages', 'email', {}),
    ('send just the messages part to Mom via email', 'messages', {}),
    ('what is in my messages and send it to Mom via Messages', 'messages', {}),
    ('send my messages summary for yesterday to Mom via Messages', 'messages', {'day': 'yesterday'}),
    ('draft my email summary for this week to Mom via Messages', 'email', {'period': 'this week'}),
    ('send my email to Mom via Messages', 'email', {}),
    ('send only the email section of my daily summary to Mom via Messages', 'email', {}),
    ('send just the messages part of my daily summary to Mom via Messages', 'messages', {}),
])
def test_explicit_subset_wins_over_broader_answer(prompt, source, args):
    plan = compile_new(prompt, last_user='daily summary', last_assistant=STALE)
    assert plan.sources == [source] and plan.artifact_text == ''
    assert plan.source_args == {source: args}


@pytest.mark.parametrize('pronoun', ['this', 'that', 'it'])
def test_plain_pronoun_requires_verified_content(pronoun):
    plan = compile_new(f'send {pronoun} to Mom via Messages',
                       last_user='daily summary', last_assistant=STALE)
    assert plan.artifact_text == '' and not plan.sources
    assert plan.status == 'waiting_for_content'


@pytest.mark.parametrize('prompt', [
    'shorten it and send it to Mom via Messages',
    'translate that into Spanish and send it to Mom via Messages',
    'send just the first bullet to Mom via Messages',
    'send it without the private details to Mom via Messages',
    'draft a shorter version of this for Mom via Messages',
    'send only the first email summary to Mom via Messages',
])
def test_unknown_transformation_cannot_offer_effect(tmp_path, prompt):
    store, sid = conversation(tmp_path)
    turn = prepare_turn(store, sid, prompt)
    assert turn.plan.status == 'waiting_for_content'
    assert turn.response and turn.decision is None and not turn.plan.artifact_text
    store.add_turn(sid, 'user', prompt)
    store.add_turn(sid, 'assistant', turn.response)
    for reply in ('Messages', 'yes', 'send it'):
        next_turn = prepare_turn(store, sid, reply)
        assert next_turn.decision is None and next_turn.response


@pytest.mark.parametrize('correction', ['only the email section', 'the email section'])
def test_pending_artifact_scope_can_be_narrowed(tmp_path, correction):
    store, sid = conversation(tmp_path)
    first = prepare_turn(store, sid, 'send my daily summary to Mom')
    assert first.plan.sources == ['daily_brief'] and not first.plan.artifact_text
    second = prepare_turn(store, sid, correction)
    assert second.plan.sources == ['email'] and second.plan.artifact_text == ''
    assert second.plan.recipient == 'Mom'
    third = prepare_turn(store, sid, 'Messages')
    assert third.decision.direct_calls[0] == ('summarize_emails', {})


def test_unrelated_prior_answer_is_not_a_named_report():
    plan = compile_new('send my email summary to Mom via Messages',
                       last_user='Tell me a joke', last_assistant='UNRELATED_JOKE')
    assert plan.sources == ['email'] and not plan.artifact_text


def test_new_range_requires_fresh_source_even_for_same_named_report():
    plan = compile_new('send my email summary for today to Mom via Messages',
                       last_user='my email summary yesterday', last_assistant='YESTERDAY')
    assert plan.sources == ['email'] and plan.source_args == {'email': {'day': 'today'}}
    assert not plan.artifact_text


@pytest.mark.parametrize('prior', ['', 'Which content should I send?', 'Nothing sent; cancelled.'])
def test_unresolved_pronoun_stays_in_workflow(prior):
    plan = compile_new('send it to Mom via Messages', last_assistant=prior)
    assert plan.status == 'waiting_for_content' and not plan.artifact_text


@pytest.mark.parametrize('sources', [[], ['email']])
def test_legacy_bad_artifact_fails_before_any_lookup(delivery, monkeypatch, sources):
    plan = WorkflowPlan(sources=sources, artifact_text=STALE,
                        original_request='what is on my email and can you send it to mom',
                        recipient='Mom', channel='messages')
    monkeypatch.setattr(executor, 'resolve_destination', lambda *a: pytest.fail('no contacts lookup'))
    result = execute(delivery, plan)
    assert result.status == 'failed'
    assert not delivery.reads and not delivery.previews and not delivery.effects


@pytest.mark.parametrize('draft', [False, True])
def test_fresh_payload_is_identical_to_confirmation(delivery, draft):
    delivery.allow = True
    plan = compile_new(('draft' if draft else 'send') + ' my email summary to Mom via Messages',
                       last_user='daily summary', last_assistant=STALE)
    result = execute(delivery, plan)
    assert result.status == 'completed'
    assert delivery.effects == [delivery.previews[0]['args']]
    assert STALE not in delivery.effects[0]['text']


def test_failed_source_never_falls_back_to_old_answer(delivery):
    delivery.source = '(error: synthetic unavailable)'
    plan = compile_new('what is on my email and send it to Mom via Messages',
                       last_user='daily summary', last_assistant=STALE)
    result = execute(delivery, plan)
    assert result.status == 'failed' and not delivery.previews and not delivery.effects


def test_message_subset_preview_excludes_daily_email_and_calendar(tmp_path, delivery):
    store, sid = conversation(tmp_path)
    delivery.source = 'FRESH_MESSAGE: Synthetic conversation'
    turn = prepare_turn(store, sid,
                        'send only the messages section of my daily summary to Mom via Messages')
    result = execute(delivery, turn.plan)
    assert [call['name'] for call in result.tool_calls] == ['summarize_messages', 'send_message']
    body = delivery.previews[0]['args']['text']
    assert 'FRESH_MESSAGE' in body
    assert all(marker not in body for marker in ('PRIVATE_EVENT', 'OLD_EMAIL', 'PRIVATE_CHAT'))
    assert not delivery.effects


def test_new_explicit_request_resolves_content_clarification(tmp_path):
    store, sid = conversation(tmp_path)
    blocked = prepare_turn(store, sid, 'shorten it and send it to Mom via Messages')
    store.add_turn(sid, 'user', blocked.plan.original_request)
    store.add_turn(sid, 'assistant', blocked.response)
    resumed = prepare_turn(store, sid, 'send my email summary to Mom via Messages')
    assert resumed.decision is not None
    assert resumed.plan.sources == ['email'] and not resumed.plan.artifact_text
    assert resumed.plan.content_error == ''


@pytest.mark.parametrize('instruction', [
    'remove names', 'redact names', 'omit names', 'anonymize it', 'strip names',
    'obfuscate identities', 'sanitize identifying details', 'make it anonymous',
])
@pytest.mark.parametrize('template', ['{instruction} and send it to Mom via Messages',
                                      'send it to Mom via Messages and {instruction}'])
def test_unsupported_reference_instructions_fail_closed(instruction, template):
    plan = compile_new(template.format(instruction=instruction),
                       last_user='daily summary', last_assistant=STALE)
    assert plan.content_error and not plan.artifact_text
    assert plan.status == 'waiting_for_content'


@pytest.mark.parametrize('reply', [
    'Messages, redact the names', 'remove names and use Messages',
    'Messages, obfuscate identities', 'send it to Mom and omit names',
    'send it to Mom and conceal the identifiers',
])
def test_pending_plan_cannot_discard_unrecognized_content_edit(tmp_path, reply):
    store, sid = conversation(tmp_path)
    first = prepare_turn(store, sid, 'send my daily summary to Mom')
    store.add_turn(sid, 'user', first.plan.original_request)
    store.add_turn(sid, 'assistant', first.response)
    edited = prepare_turn(store, sid, reply)
    assert edited is not None and edited.decision is None
    assert edited.plan.status == 'waiting_for_content'
    assert not edited.plan.artifact_text
    assert prepare_turn(store, sid, 'Messages').decision is None


@pytest.mark.parametrize('noun,source', [
    ('weather', 'weather'), ('reminders', 'reminder'), ('news', 'news'), ('stocks', 'stock'),
    ('email', 'email'), ('messages', 'messages'), ('calendar', 'calendar'),
])
def test_every_supported_daily_subset_replaces_umbrella(noun, source):
    plan = compile_new(f'send only the {noun} section of my daily summary to Mom via Messages',
                       last_user='daily summary', last_assistant=STALE)
    assert plan.sources == [source]
    assert 'daily_brief' not in plan.sources and not plan.artifact_text


@pytest.mark.parametrize('prompt', [
    'send only the finance section of my daily summary to Mom via Messages',
    'conceal identities in my daily summary and send it to Mom via Messages',
    'obfuscate names and send my daily summary to Mom via Messages',
])
def test_unknown_named_report_instruction_clarifies(prompt):
    plan = compile_new(prompt, last_user='daily summary', last_assistant=STALE)
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('tail,recipient,mode,when', [
    ('to Dad via Messages as a draft', 'Dad', 'draft', ''),
    ('to Alex via Messages at 11 pm', 'Alex', 'scheduled', 'at 11 pm'),
    ('to Dad via Messages send now', 'Dad', 'send', ''),
    ('via Messages', 'Mom', 'scheduled', 'at 9 pm'),
])
def test_subset_correction_inherits_only_omitted_delivery_fields(tmp_path, tail, recipient, mode, when):
    store, sid = conversation(tmp_path)
    first = prepare_turn(store, sid, 'schedule send my daily summary to Mom at 9 pm')
    assert first.plan.delivery == 'scheduled' and first.plan.when == 'at 9 pm'
    correction = prepare_turn(store, sid, 'only the email section ' + tail)
    assert correction.plan.recipient == recipient
    assert correction.plan.channel == 'messages'
    assert correction.plan.delivery == mode
    assert correction.plan.when == when
    assert correction.plan.sources == ['email']


def test_content_clarification_is_recovered_after_one_hour(tmp_path, monkeypatch):
    import time
    store, sid = conversation(tmp_path)
    first = prepare_turn(store, sid, 'redact names and send it to Mom via Messages')
    now = time.time()
    monkeypatch.setattr('service.workflows.engine.time.time', lambda: now + 3600)
    recovered = prepare_turn(store, sid, 'Messages')
    assert recovered is not None and recovered.decision is None
    assert recovered.plan.id == first.plan.id
    assert recovered.plan.status == 'waiting_for_content'


@pytest.mark.parametrize('effect,channel', [('send_message', 'messages'), ('send_email', 'email'),
                                            ('draft_email', 'email'), ('draft_message', 'messages')])
@pytest.mark.parametrize('allow', [False, True])
def test_production_effect_receives_exact_canonical_preview(monkeypatch, effect, channel, allow):
    from service.tools import action_tools
    native_calls, previews, events = [], [], []

    async def app_request(name, args):
        native_calls.append((name, args))
        return {'ok': True}

    async def confirm(action):
        previews.append(action)
        return allow

    async def emit(event):
        events.append(event)

    # Keep real registry tools and production action implementations. Replace
    # only the native bridge and identity lookup: no app can be opened/sent to.
    monkeypatch.setattr(action_tools, 'app_request', app_request)
    monkeypatch.setattr(action_tools, '_own_address_guard', lambda *a, **k: None)
    destination = 'fixture@example.test' if channel == 'email' else '+15555550123'
    plan = WorkflowPlan(recipient=destination, channel=channel,
                        delivery='draft' if effect.startswith('draft') else 'send',
                        artifact_provenance='tool_receipt',
                        artifact_text="  Line one\\nLine two\\twith \\\"quotes\\\" and \\\\'nested\\\\'.  ")
    result = execute(SimpleNamespace(emit=emit, approver=SimpleNamespace(confirm=confirm)), plan)
    assert len(previews) == 1
    key = 'text' if channel == 'messages' else 'body'
    approved_body = previews[0]['args'][key]
    assert '\n' in approved_body and '\\n' not in approved_body
    assert action_tools._degarble(approved_body) == approved_body
    assert previews[0]['preview'].endswith(approved_body)
    if not allow:
        assert result.status == 'denied' and not native_calls
    elif effect == 'draft_message':
        assert result.status == 'completed' and not native_calls
        assert next(e['text'] for e in events if e['type'] == 'message_draft') == approved_body
    else:
        assert result.status == 'completed'
        assert native_calls == [(effect, native_calls[0][1])]
        assert native_calls[0][1][key] == approved_body


@pytest.mark.parametrize('channel', ['messages', 'email'])
@pytest.mark.parametrize('allow', [False, True])
def test_production_scheduled_payload_matches_preview(monkeypatch, channel, allow):
    from service.assistant.outbound_queue import outbound_queue
    queued, previews = [], []

    def add(**kwargs):
        queued.append(kwargs)
        return 'synthetic-queue-id'

    async def confirm(action):
        previews.append(action)
        return allow

    async def emit(event):
        pass

    monkeypatch.setattr(outbound_queue, 'add', add)
    plan = WorkflowPlan(recipient='fixture@example.test', channel=channel,
                        delivery='scheduled', when='2099-01-01T12:00:00+00:00',
                        artifact_provenance='tool_receipt',
                        artifact_text='  Synthetic\\nbody with \\\\' + "'nested\\\\'.  ")
    result = execute(SimpleNamespace(emit=emit, approver=SimpleNamespace(confirm=confirm)), plan)
    assert len(previews) == 1
    approved = previews[0]['args']['body']
    assert previews[0]['preview'].endswith(approved)
    if allow:
        assert result.status == 'completed'
        assert queued[0]['body'] == approved
    else:
        assert result.status == 'denied' and not queued


@pytest.mark.parametrize('prompt,mode,when', [
    ('draft it to Dad via Messages', 'draft', ''),
    ('send it to Dad via Messages now', 'send', ''),
    ('send it to Dad via Messages at 11 pm', 'scheduled', 'at 11 pm'),
])
def test_plain_reference_correction_preserves_explicit_delivery(tmp_path, prompt, mode, when):
    store, sid = conversation(tmp_path)
    first = prepare_turn(store, sid, 'schedule send my daily summary to Mom at 9 pm')
    store.add_turn(sid, 'user', first.plan.original_request)
    store.add_turn(sid, 'assistant', first.response)
    correction = prepare_turn(store, sid, prompt)
    assert correction.plan.recipient == 'Dad'
    assert correction.plan.delivery == mode and correction.plan.when == when
    assert correction.plan.channel == 'messages'


@pytest.mark.parametrize('reply', ['anonymize it', 'remove names', 'Mom, redact names',
                                  'conceal the identifiers'])
def test_pending_recipient_cannot_consume_content_edit_as_name(tmp_path, reply):
    store, sid = conversation(tmp_path)
    first = prepare_turn(store, sid, 'send my daily summary via Messages')
    assert first.plan.status == 'waiting_for_recipient'
    store.add_turn(sid, 'user', first.plan.original_request)
    store.add_turn(sid, 'assistant', first.response)
    edited = prepare_turn(store, sid, reply)
    assert edited.plan.status == 'waiting_for_content'
    assert not edited.plan.artifact_text and edited.decision is None


@pytest.mark.parametrize('prompt', [
    'send the weather from my daily summary to Mom via Messages',
    'send only the weather and news from my daily summary to Mom via Messages',
])
def test_ambiguous_daily_subset_cannot_keep_umbrella_or_drop_requested_source(prompt):
    plan = compile_new(prompt, last_user='daily summary', last_assistant=STALE)
    assert plan.status == 'waiting_for_content' and not plan.artifact_text


@pytest.mark.parametrize('last_user', ['summarize my inbox', 'give me my email summary'])
def test_same_requested_source_does_not_prove_assistant_artifact(last_user, delivery):
    plan = compile_new('send my email summary to Mom via Messages',
                       last_user=last_user, last_assistant=STALE)
    assert plan.sources == ['email'] and not plan.artifact_text
    result = execute(delivery, plan)
    assert result.status == 'denied'
    assert 'FRESH_EMAIL' in delivery.previews[0]['args']['text']
    assert STALE not in delivery.previews[0]['args']['text']


@pytest.mark.parametrize('modifier', [
    'redacted', 'sanitized', 'sanitised', 'anonymous', 'anonymized', 'anonymised',
    'redacting', 'sanitizing', 'names removed', 'names omitted', 'identifiers stripped',
    'identities masked', 'de-identified', 'identifiers hidden', 'names concealed',
    'censored', 'scrubbed', 'cleaned', 'privacy-preserving', 'discreet',
])
def test_direct_modified_source_request_fails_closed_without_context(modifier):
    plan = compile_new(f'send my {modifier} email summary to Mom via Messages')
    assert plan is not None and plan.status == 'waiting_for_content'
    assert not plan.artifact_text


@pytest.mark.parametrize('delivery_mode', ['scheduled', 'draft'])
@pytest.mark.parametrize('correction', ['email section to Dad via Messages',
                                       'email summary to Dad via Messages',
                                       'calendar section to Dad via Messages'])
def test_independently_compiled_scope_correction_keeps_mode(tmp_path, delivery_mode, correction):
    store, sid = conversation(tmp_path)
    request = ('schedule send my daily summary to Mom at 9 pm' if delivery_mode == 'scheduled'
               else 'draft my daily summary to Mom')
    first = prepare_turn(store, sid, request)
    assert first.plan.delivery == delivery_mode
    changed = prepare_turn(store, sid, correction)
    assert changed.plan.recipient == 'Dad' and changed.plan.channel == 'messages'
    assert changed.plan.delivery == delivery_mode
    assert changed.plan.when == ('at 9 pm' if delivery_mode == 'scheduled' else '')


@pytest.mark.parametrize('age,active', [(21599.999, True), (21600, True), (21600.001, False)])
def test_content_clarification_six_hour_boundary_after_reopen(tmp_path, monkeypatch, age, active):
    # Six hours is inclusive, matching SessionStore's existing expiry rule.
    clock = [2000000000.0]
    monkeypatch.setattr('service.workflows.engine.time.time', lambda: clock[0])
    path = tmp_path / 'reopened.db'
    store = SessionStore(path)
    sid = store.create_session()
    first = prepare_turn(store, sid, 'send a redacted email summary to Mom via Messages')
    assert first.plan.status == 'waiting_for_content'
    store._db.close()
    clock[0] += age
    reopened = SessionStore(path)
    try:
        reply = prepare_turn(reopened, sid, 'Messages')
        if active:
            assert reply is not None and reply.plan.id == first.plan.id
            assert reply.plan.status == 'waiting_for_content' and reply.decision is None
        else:
            assert reply is None
    finally:
        reopened._db.close()


def test_effect_requires_durable_store_before_any_lookup(delivery, monkeypatch):
    plan = compile_new('send my email summary to Mom via Messages')
    plan.status = 'running'
    monkeypatch.setattr(executor, 'resolve_destination', lambda *a: pytest.fail('must fail before lookup'))
    result = asyncio.run(executor.execute_workflow(plan, delivery.emit, delivery.approver))
    assert result.status == 'failed'
    assert not delivery.effects and not delivery.previews


def test_claim_failure_cannot_invoke_effect(tmp_path, delivery, monkeypatch):
    store = SessionStore(tmp_path / 'claims.db')
    def fail(*args, **kwargs):
        raise OSError('synthetic persistence failure')
    monkeypatch.setattr(store, 'claim_workflow_effect', fail)
    delivery.allow = True
    plan = compile_new('send my email summary to Mom via Messages')
    plan.status = 'running'
    sid = store.create_session()
    store.save_workflow(sid, plan.to_dict())
    result = asyncio.run(executor.execute_workflow(
        plan, delivery.emit, delivery.approver, store=store, session_id=sid))
    assert result.status == 'failed' and not delivery.effects
    store._db.close()


def test_denial_does_not_consume_effect_claim(tmp_path, delivery):
    store = SessionStore(tmp_path / 'claims.db')
    plan = compile_new('send my email summary to Mom via Messages')
    plan.status = 'running'
    sid = store.create_session()
    store.save_workflow(sid, plan.to_dict())
    result = asyncio.run(executor.execute_workflow(
        plan, delivery.emit, delivery.approver, store=store, session_id=sid))
    assert result.status == 'denied' and not store.workflow_effect_claimed(plan.id)
    store._db.close()


@pytest.mark.parametrize('elapsed', [301, 21601])
def test_process_crash_after_external_success_cannot_replay(tmp_path, monkeypatch, delivery, elapsed):
    import subprocess
    import sys
    import time
    from textwrap import dedent

    path = tmp_path / 'crash.db'
    marker = tmp_path / 'synthetic-external-effect.txt'
    store = SessionStore(path)
    sid = store.create_session()
    turn = prepare_turn(store, sid, 'send my email summary to +15555550123 via Messages')
    assert turn.plan.status == 'running'
    store._db.close()
    child = dedent('''
        import asyncio, os, sys
        from pathlib import Path
        from types import SimpleNamespace
        from service.memory.store import SessionStore
        from service.workflows.models import WorkflowPlan
        from service.workflows import executor
        from service.tools.registry import REGISTRY, Tool
        from service.safety.policy import Decision, Tier
        store = SessionStore(Path(sys.argv[1]))
        plan = WorkflowPlan.from_dict(store.active_workflow(sys.argv[2]))
        async def read(**kwargs):
            return 'Synthetic inbox result.'
        async def effect(**kwargs):
            Path(sys.argv[3]).write_text(kwargs['text'])
            os._exit(73)  # external success, before tool_result/finalization
        async def emit(event):
            pass
        async def approve(action):
            return True
        REGISTRY['summarize_emails'] = Tool('summarize_emails', 'synthetic',
            {'properties': {}}, 'assistant_read', read)
        REGISTRY['send_message'] = Tool('send_message', 'synthetic',
            {'properties': {'to': {'type': 'string'}, 'text': {'type': 'string'}}},
            'messages_send', effect)
        executor.decide = lambda *a, **k: Decision(Tier.ALLOW, 'synthetic')
        asyncio.run(executor.execute_workflow(plan, emit, SimpleNamespace(confirm=approve),
                    store=store, session_id=sys.argv[2]))
    ''')
    crashed = subprocess.run([sys.executable, '-c', child, str(path), sid, str(marker)],
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 73, crashed.stderr
    assert marker.read_text() == 'Email:\nSynthetic inbox result.'
    now = time.time()
    monkeypatch.setattr('service.workflows.engine.time.time', lambda: now + elapsed)
    reopened = SessionStore(path)
    try:
        assert reopened.workflow_effect_claimed(turn.plan.id)
        for prompt in ('yes', 'try again', 'send it to Dad via Messages',
                       'email section to Dad via Messages',
                       'send my email summary to Mom via Messages'):
            retry = prepare_turn(reopened, sid, prompt)
            assert retry.event == 'uncertain_delivery_blocked' and retry.decision is None
            assert retry.plan.id == turn.plan.id
        prepare_turn(reopened, sid, 'cancel')
        retry = prepare_turn(reopened, sid, 'send it to Dad via Messages')
        assert retry.decision is None
        # Even bypassing conversation preparation with different read counts
        # cannot run the claimed plan's effect a second time.
        turn.plan.sources = ['email', 'messages']
        delivery.allow = True
        result = asyncio.run(executor.execute_workflow(
            turn.plan, delivery.emit, delivery.approver, store=reopened))
        assert result.status == 'failed'
        assert not delivery.reads and not delivery.previews and not delivery.effects
        assert marker.read_text() == 'Email:\nSynthetic inbox result.'
    finally:
        reopened._db.close()


def test_two_executors_share_one_durable_effect_claim(tmp_path, delivery):
    first = SessionStore(tmp_path / 'concurrent.db')
    second = SessionStore(tmp_path / 'concurrent.db')
    delivery.allow = True
    plan = compile_new('send my email summary to Mom via Messages')
    plan.status = 'running'
    sid = first.create_session()
    first.save_workflow(sid, plan.to_dict())
    async def race():
        barrier = asyncio.Event()
        arrivals = 0
        async def simultaneous_approval(action):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                barrier.set()
            await barrier.wait()
            return True
        return await asyncio.gather(*(
            executor.execute_workflow(plan, delivery.emit,
                                      SimpleNamespace(confirm=simultaneous_approval), store=store,
                                      session_id=sid)
            for store in (first, second)))
    results = asyncio.run(race())
    assert sorted(result.status for result in results) == ['completed', 'failed']
    assert len(delivery.effects) == 1 and first.workflow_effect_claimed(plan.id)
    first._db.close()
    second._db.close()


@pytest.mark.parametrize('adverb', ['only', 'just'])
@pytest.mark.parametrize('verb', ['text', 'send', 'message', 'email'])
@pytest.mark.parametrize('allow', [False, True])
def test_leading_delivery_adverb_keeps_daily_source(tmp_path, delivery, monkeypatch, adverb, verb, allow):
    store, sid = conversation(tmp_path)
    reads = []
    async def daily(**args):
        reads.append(args)
        return 'FRESH_DAILY: Synthetic calendar, inbox and conversations.'
    monkeypatch.setitem(REGISTRY, 'daily_brief', Tool('daily_brief', 'synthetic', {
        'properties': {'days': {'type': 'integer'}}}, 'assistant_read', daily))
    prompt = f'{adverb} {verb} my daily summary to Mom via Messages'
    turn = prepare_turn(store, sid, prompt)
    assert turn.plan.sources == ['daily_brief'] and not turn.plan.content_error
    assert turn.plan.source_args == {'daily_brief': {'days': 1}}
    delivery.allow = allow
    result = execute(delivery, turn.plan)
    assert reads == [{'days': 1}] and not delivery.reads
    assert [call['name'] for call in result.tool_calls] == ['daily_brief', 'send_message']
    assert 'FRESH_DAILY' in delivery.previews[-1]['args']['text']
    assert ('PRIVATE_CHAT' not in delivery.previews[-1]['args']['text'])
    assert result.status == ('completed' if allow else 'denied')
    assert delivery.effects == ([delivery.previews[-1]['args']] if allow else [])
    store._db.close()


@pytest.mark.parametrize('prompt', [
    'send only the messages from Alice to Mom via Messages',
    'send my messages from Alice to Bob via email',
    'send my messages from Alice yesterday to Mom via Messages',
    'send a summary of messages from Alice to Bob via email',
    "send Alice's messages to Mom via Messages",
    'send my messages yesterday from Alice to Mom via Messages',
    'send my messages from yesterday from Alice to Mom via Messages',
    'send my messages from last week with Alice to Mom via Messages',
    'send my text yesterday from Alice to Mom via Messages',
    'send my message last week from Alice to Mom via Messages',
    'send a summary of the message sent by Alice to Mom via Messages',
    'send a summary of the message written by Alice to Mom via Messages',
    'send my messages Alice sent yesterday to Mom via Messages',
    'send my messages yesterday Alice wrote to Mom via Messages',
    'send my messages that Alice sent last week to Mom via Messages',
    'send my message Alice wrote yesterday to Mom via Messages',
    'send my messages written by Alice yesterday to Mom via Messages',
    "send Alice’s messages to Mom via Messages",
    'send Mary Jane Smith’s texts to Mom via Messages',
])
def test_sender_scope_clarifies_before_any_read_or_effect(tmp_path, delivery, prompt):
    store, sid = conversation(tmp_path)
    turn = prepare_turn(store, sid, prompt)
    assert turn is not None and turn.plan.status == 'waiting_for_content'
    assert turn.decision is None and turn.response
    result = execute(delivery, turn.plan)
    assert result.status == 'failed' and 'content scope is unresolved' in result.response
    assert not delivery.reads and not delivery.previews and not delivery.effects
    store.add_turn(sid, 'user', prompt)
    store.add_turn(sid, 'assistant', turn.response)
    store._db.close()
    store = SessionStore(tmp_path / 'sessions.db')
    for reply in ('Messages', 'yes', '+15555550123'):
        followup = prepare_turn(store, sid, reply)
        assert followup is not None and followup.decision is None
        assert followup.plan.status == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('period,args', [
    ('yesterday', {'day': 'yesterday'}), ('last week', {'period': 'last week'}),
    ('past 3 days', {'period': 'past 3 days'}),
])
def test_temporal_message_scope_still_uses_supported_tool(period, args):
    plan = compile_new(f'send my messages from {period} to Mom via Messages')
    assert plan.sources == ['messages'] and not plan.content_error
    assert plan.source_args == {'messages': args}


@pytest.mark.parametrize('prompt,source,args', [
    ('send only my unread emails to Mom via Messages', 'email', {'unread': True}),
    ("send my emails that I haven't read to Mom via Messages", 'email', {'unread': True}),
    ('send my emails from my Work account to Mom via Messages', 'email', {'account': 'Work'}),
    ('send my unread emails from my Work account to Mom via Messages', 'email',
     {'unread': True, 'account': 'Work'}),
    ('send my messages with Alice to Mom via email', 'messages', {'conversation': 'Alice'}),
    ('send my messages with Family Chat from yesterday to Mom via email', 'messages',
     {'day': 'yesterday', 'conversation': 'Family Chat'}),
    ('send my messages in the Family Chat conversation to Mom via email', 'messages',
     {'conversation': 'Family Chat'}),
])
def test_supported_private_source_qualifiers_are_exact(prompt, source, args):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == [source] and plan.source_args == {source: args}


@pytest.mark.parametrize('prompt', [
    'send my emails from Alice to Mom via Messages',
    'send my emails about payroll to Mom via Messages',
    'send my starred emails to Mom via Messages',
    'send my unread emails from yesterday to Mom via Messages',
    'send my messages about dinner to Mom via email',
    'send my unread messages to Mom via email',
    'send my messages from Alice to Mom via email',
    'send my emails from today and yesterday to Mom via Messages',
])
def test_unsupported_private_source_qualifiers_wait_for_content(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt', [
    'send my emails from Alice to Mom via Messages',
    'send my emails about payroll to Mom via Messages',
    'send my starred emails to Mom via Messages',
    'send my unread emails from yesterday to Mom via Messages',
    'send my messages about dinner to Mom via email',
    'send my unread messages to Mom via email',
])
def test_unsupported_qualifier_stops_before_read_at_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt):
    from service import main
    from service.memory import context
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Unresolved source qualifier escaped the workflow boundary')
    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    asyncio.run(agent_events(main, sid, prompt))
    assert not delivery.reads and not delivery.previews and not delivery.effects
    assert store.latest_workflow(sid)['status'] == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('prompt,args', [
    ('send only my unread emails to +15555550123 via Messages', {'unread': True}),
    ('send my messages with Alice from yesterday to +15555550123 via Messages',
     {'day': 'yesterday', 'conversation': 'Alice'}),
])
def test_supported_qualifier_reaches_exact_source_at_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt, args):
    from service import main
    from service.memory import context
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Scoped request escaped the workflow boundary')
    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    asyncio.run(agent_events(main, sid, prompt))
    assert delivery.reads == [args]
    assert len(delivery.previews) == 1 and not delivery.effects
    store._db.close()


def test_stale_revision_stops_before_private_read(tmp_path, delivery):
    store, sid = conversation(tmp_path)
    turn = prepare_turn(store, sid, 'send my email summary to +15555550123 via Messages')
    stale = WorkflowPlan.from_dict(turn.plan.to_dict())
    prepare_turn(store, sid, 'cancel')
    result = asyncio.run(executor.execute_workflow(
        stale, delivery.emit, delivery.approver, store=store, session_id=sid))
    assert result.status == 'failed'
    assert not delivery.reads and not delivery.previews and not delivery.effects
    store._db.close()


@pytest.mark.parametrize('interruption,expected_status,same_plan', [
    ('cancel', 'cancelled', True),
    ('send a redacted email summary to Mom via Messages', 'waiting_for_content', False),
    ('send my calendar summary', 'waiting_for_channel', False),
])
def test_agent_boundary_stale_approval_cannot_send_or_overwrite_newer_state(
        tmp_path, monkeypatch, delivery, interruption, expected_status, same_plan):
    from service import main
    from service.memory import context
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    entered = asyncio.Event()
    release = asyncio.Event()

    class HeldApproval:
        async def confirm(self, action):
            delivery.previews.append(action)
            entered.set()
            await release.wait()
            return True

    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: HeldApproval())
    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Workflow request escaped to ordinary routing')
    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)

    async def race():
        original = asyncio.create_task(agent_events(
            main, sid, 'send my email summary to +15555550123 via Messages'))
        await entered.wait()
        running = store.latest_workflow(sid, max_age_seconds=float('inf'))
        await agent_events(main, sid, interruption)
        newer = store.latest_workflow(sid, max_age_seconds=float('inf'))
        release.set()
        await original
        return running, newer, store.latest_workflow(sid, max_age_seconds=float('inf'))

    running, newer, final = asyncio.run(race())
    assert newer['status'] == expected_status
    assert (newer['id'] == running['id']) is same_plan
    assert final['id'] == newer['id'] and final['status'] == expected_status
    assert final['revision'] == newer['revision']
    assert len(delivery.reads) == 1 and not delivery.effects
    store._db.close()


@pytest.mark.parametrize('prompt', [
    'please only text my daily summary to Mom via Messages',
    'can you just send my daily summary to Mom via Messages',
])
def test_polite_delivery_emphasis_is_not_a_source_subset(prompt):
    plan = compile_new(prompt)
    assert plan.sources == ['daily_brief'] and not plan.content_error


def test_quoted_sender_words_are_not_a_scope_instruction():
    from service.workflows.compiler import _unsupported_message_sender
    assert not _unsupported_message_sender('send "messages from Alice are funny" to Mom')
    assert _unsupported_message_sender('send messages from "Alice" to Mom')


@pytest.mark.parametrize('prompt', [
    'email Mom "I will arrive at six"',
    'draft an email to Mom saying "hello"',
])
def test_quoted_email_literal_body_stays_on_ordinary_endpoint(
        tmp_path, monkeypatch, delivery, prompt):
    from service import main
    from service.memory import context

    assert compile_new(prompt) is None
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    reached = []

    async def literal_route(text, **kwargs):
        reached.append(text)
        raise RuntimeError('synthetic literal-route boundary')

    async def no_inference(*unused_args, **unused_kwargs):
        raise AssertionError('Unexpected inference before literal route')

    async def no_source(**kwargs):
        raise AssertionError('Quoted literal caused a private email read')

    monkeypatch.setattr(main, 'route', literal_route)
    monkeypatch.setattr(main, 'ensure_omlx', no_inference)
    monkeypatch.setitem(REGISTRY, 'summarize_emails', Tool(
        'summarize_emails', 'tripwire', {'properties': {}},
        'assistant_read', no_source))
    events = asyncio.run(agent_events(main, sid, prompt))

    assert reached == [prompt]
    assert not any(event['type'] in {'workflow', 'tool_call', 'task_plan'}
                   for event in events)
    assert not delivery.reads and not delivery.previews and not delivery.effects
    store._db.close()


@pytest.mark.parametrize('opening,closing,period,args', [
    ('"', '"', 'yesterday', {'day': 'yesterday'}),
    ('“', '”', 'last week', {'period': 'last week'}),
    ("'", "'", 'yesterday', {'day': 'yesterday'}),
    ('‘', '’', 'last week', {'period': 'last week'}),
])
def test_quoted_temporal_scope_is_preserved_for_explicit_messages_source(
        opening, closing, period, args):
    plan = compile_new(f'send my messages for {opening}{period}{closing} to Mom via Messages')
    assert plan is not None and plan.sources == ['messages']
    assert not plan.content_error and plan.source_args == {'messages': args}


@pytest.mark.parametrize('prompt', [
    'send my messages "from Alice" to Mom via Messages',
    'send my messages “from Alice” to Mom via Messages',
    "send my messages 'with Alice' to Mom via Messages",
    'send my messages ‘with Alice’ to Mom via Messages',
])
def test_unknown_quoted_message_scope_clarifies_before_any_read_or_effect(tmp_path, delivery, prompt):
    store, sid = conversation(tmp_path)
    turn = prepare_turn(store, sid, prompt)
    assert turn is not None and turn.plan.status == 'waiting_for_content'
    assert turn.decision is None and turn.response
    result = execute(delivery, turn.plan)
    assert result.status == 'failed' and 'content scope is unresolved' in result.response
    assert not delivery.reads and not delivery.previews and not delivery.effects
    store._db.close()


@pytest.mark.parametrize('prompt,is_daily', [
    ('only text my daily summary to Mom via Messages', True),
    ('just text my daily summary to Mom via Messages', True),
    ('send only the messages from Alice to Mom via Messages', False),
    ('send a summary of messages from Alice to Bob via email', False),
    ('send my messages yesterday from Alice to Mom via Messages', False),
    ("send Alice's messages to Mom via Messages", False),
    ('send a summary of the message sent by Alice to Mom via Messages', False),
    ('send a summary of the message written by Alice to Mom via Messages', False),
    ('send my messages Alice sent yesterday to Mom via Messages', False),
    ('send my messages that Alice wrote last week to Mom via Messages', False),
    ('send my messages written by Alice yesterday to Mom via Messages', False),
    ("send Mary Jane Smith’s texts to Mom via Messages", False),
    ('send my messages "from Alice" to Mom via Messages', False),
])
def test_reported_source_scopes_at_real_agent_boundary(tmp_path, monkeypatch, delivery, prompt, is_daily):
    import json
    from service import main
    from service.memory import context
    from service.tasks import engine as task_engine
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Source request escaped to ordinary routing or inference')
    def no_contact(*args, **kwargs):
        raise AssertionError('Unnecessary typed-task contact lookup')
    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    monkeypatch.setattr(task_engine, '_default_contacts_resolver', no_contact)
    async def daily(**args):
        delivery.reads.append(args)
        return 'FRESH_DAILY: Calendar, inbox and conversations.'
    monkeypatch.setitem(REGISTRY, 'daily_brief', Tool('daily_brief', 'synthetic', {
        'properties': {'days': {'type': 'integer'}}}, 'assistant_read', daily))
    async def request(text):
        response = await main.agent({'prompt': text, 'session_id': sid, 'debug': False})
        events = []
        async for item in response.body_iterator:
            if isinstance(item, bytes): item = item.decode()
            events.append(json.loads(item.removeprefix('data: ').strip()))
        assert not any(e['type'] in {'error', 'task_plan', 'routed'} for e in events), events
        assert any(e['type'] == 'workflow' for e in events)
    asyncio.run(request(prompt))
    if is_daily:
        assert delivery.reads == [{'days': 1}]
        assert len(delivery.previews) == 1 and not delivery.effects
        assert 'FRESH_DAILY' in delivery.previews[0]['args']['text']
        assert store.latest_workflow(sid)['status'] == 'cancelled'
    else:
        assert not delivery.reads and not delivery.previews and not delivery.effects
        for reply in ('Messages', 'yes', '+15555550123'):
            asyncio.run(request(reply))
            assert store.latest_workflow(sid)['status'] == 'waiting_for_content'
        assert not delivery.reads and not delivery.previews and not delivery.effects
    store._db.close()


@pytest.mark.parametrize('modifier', ['last', 'this', 'next', 'past'])
@pytest.mark.parametrize('unit', ['day', 'week', 'month', 'year'])
@pytest.mark.parametrize('apostrophe', ["'", '’'])
def test_temporal_possessive_is_not_a_sender_filter(modifier, unit, apostrophe):
    period = f'{modifier} {unit}'
    plan = compile_new(f'send {period}{apostrophe}s messages to Mom via Messages')
    assert plan is not None and plan.sources == ['messages']
    assert not plan.content_error and plan.source_args == {'messages': {'period': period}}


@pytest.mark.parametrize('period', ['last week', 'this week', 'past month'])
def test_temporal_possessive_reaches_source_and_exact_preview_at_endpoint(tmp_path, monkeypatch, delivery, period):
    import json
    from service import main
    from service.memory import context
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Temporal possessive escaped the source workflow')
    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    delivery.source = 'FRESH_PERIOD_MESSAGES: Synthetic permitted period.'
    delivery.allow = True
    async def request():
        response = await main.agent({'prompt': f"send {period}'s messages to Mom via Messages",
                                     'session_id': sid, 'debug': False})
        events = []
        async for item in response.body_iterator:
            if isinstance(item, bytes): item = item.decode()
            events.append(json.loads(item.removeprefix('data: ').strip()))
        assert not any(e['type'] in {'error', 'task_plan', 'routed'} for e in events), events
    asyncio.run(request())
    assert delivery.reads == [{'period': period}]
    assert len(delivery.effects) == 1
    assert delivery.effects == [delivery.previews[-1]['args']]
    assert 'FRESH_PERIOD_MESSAGES' in delivery.effects[0]['text']
    store._db.close()


@pytest.mark.parametrize('opening,closing,period,args', [
    ('"', '"', 'yesterday', {'day': 'yesterday'}),
    ('“', '”', 'last week', {'period': 'last week'}),
    ("'", "'", 'yesterday', {'day': 'yesterday'}),
    ('‘', '’', 'last week', {'period': 'last week'}),
])
def test_quoted_temporal_scope_reaches_exact_source_at_agent_boundary(
        tmp_path, monkeypatch, delivery, opening, closing, period, args):
    import json
    from service import main
    from service.memory import context
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Quoted temporal scope escaped the source workflow')
    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    delivery.source = 'FRESH_QUOTED_SCOPE: Synthetic permitted period.'
    delivery.allow = True
    prompt = f'send my messages for {opening}{period}{closing} to Mom via Messages'
    async def request():
        response = await main.agent({'prompt': prompt, 'session_id': sid, 'debug': False})
        events = []
        async for item in response.body_iterator:
            if isinstance(item, bytes): item = item.decode()
            events.append(json.loads(item.removeprefix('data: ').strip()))
        assert not any(e['type'] in {'error', 'task_plan', 'routed'} for e in events), events
    asyncio.run(request())
    assert delivery.reads == [args]
    assert len(delivery.effects) == 1
    assert delivery.effects == [delivery.previews[-1]['args']]
    assert 'FRESH_QUOTED_SCOPE' in delivery.effects[0]['text']
    store._db.close()


@pytest.mark.parametrize('prompt', [
    'send my calendar with my messages to Mom via email',
    'send my messages with my calendar to Mom via Messages',
    'send a summary from my messages to Mom via Messages',
])
def test_explicit_source_coordination_is_not_a_sender_filter(prompt):
    plan = compile_new(prompt)
    assert plan is not None and not plan.content_error
    assert 'messages' in plan.sources
    assert plan.source_args['messages'] == {}
    if 'calendar' in prompt:
        assert set(plan.sources) == {'calendar', 'messages'}


@pytest.mark.parametrize('prompt', [
    'send my calendar with my messages to Mom via email',
    'send my messages with my calendar to Mom via Messages',
])
def test_source_coordination_preserves_unqualified_messages_at_agent_boundary(
        tmp_path, monkeypatch, delivery, prompt):
    from service import main
    from service.memory import context
    calls = []

    async def calendar_read(**kwargs):
        calls.append(('get_upcoming', kwargs))
        return 'CALENDAR: Synthetic event'

    async def messages_read(**kwargs):
        calls.append(('summarize_messages', kwargs))
        return 'MESSAGES: Synthetic conversation'

    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {'days': {'type': 'integer'}}},
        'calendar_read', calendar_read))
    monkeypatch.setitem(REGISTRY, 'summarize_messages', Tool(
        'summarize_messages', 'synthetic',
        {'properties': {'day': {'type': 'string'}, 'period': {'type': 'string'},
                        'conversation': {'type': 'string'}}},
        'assistant_read', messages_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    fallbacks = []

    async def forbidden(*unused_args, **unused_kwargs):
        fallbacks.append(True)
        raise AssertionError('Coordinated sources escaped the workflow boundary')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    asyncio.run(agent_events(main, sid, prompt))

    assert dict(calls) == {'get_upcoming': {'days': 7}, 'summarize_messages': {}}
    assert not fallbacks and len(delivery.previews) == 1 and not delivery.effects
    store._db.close()


@pytest.mark.parametrize('opening,closing', [('"', '"'), ('“', '”'), ("'", "'"), ('‘', '’')])
@pytest.mark.parametrize('apostrophe', ["'", '’'])
def test_quoted_temporal_possessive_stays_literal_at_compiler_and_endpoint(
        tmp_path, monkeypatch, delivery, opening, closing, apostrophe):
    import json
    from service import main
    from service.memory import context
    prompt = f'send {opening}last week{apostrophe}s messages were funny{closing} to +15555550123 via Messages'
    assert compile_new(prompt) is None
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'models_config', lambda: {'tool_retrieval': {'provider': 'lexical'}})
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)
    reached = []
    async def literal_route(text, **kwargs):
        reached.append(text)
        # Stop at the ordinary route boundary; never run an effect or model.
        raise RuntimeError('synthetic literal-route boundary')
    monkeypatch.setattr(main, 'route', literal_route)
    async def no_inference(*args, **kwargs):
        raise AssertionError('Unexpected inference before literal route')
    monkeypatch.setattr(main, 'ensure_omlx', no_inference)
    async def no_source(**kwargs):
        raise AssertionError('Quoted literal caused a private Messages read')
    monkeypatch.setitem(REGISTRY, 'summarize_messages', Tool('summarize_messages', 'tripwire', {
        'properties': {'day': {'type': 'string'}, 'period': {'type': 'string'}}},
        'assistant_read', no_source))
    async def request():
        response = await main.agent({'prompt': prompt, 'session_id': sid, 'debug': False})
        events = []
        async for item in response.body_iterator:
            if isinstance(item, bytes): item = item.decode()
            events.append(json.loads(item.removeprefix('data: ').strip()))
        assert not any(e['type'] in {'workflow', 'tool_call', 'task_plan'} for e in events), events
    asyncio.run(request())
    assert reached == [prompt]
    assert not delivery.reads and not delivery.previews and not delivery.effects
    store._db.close()


@pytest.mark.parametrize('residue', ['from Work', 'incomplete ones'])
@pytest.mark.parametrize('tail', ['', ', only due today'])
def test_pre_address_source_residue_is_never_discarded(residue, tail):
    plan = compile_new(
        'send my reminders and the calendar section, '
        f'{residue}, to Mom via Messages{tail}')
    assert plan is not None and plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


SOURCE_SELECTION_MATRIX_PHRASES = {
    'calendar': 'the calendar section',
    'reminder': 'the reminders section',
    'email': 'the email section',
    'messages': 'the messages section',
    'stock': 'my stocks',
    'news': 'the news section',
    'weather': 'the weather section',
}
SOURCE_SELECTION_MATRIX_CASES = [
    (left, right, selected)
    for left in SOURCE_SELECTION_MATRIX_PHRASES
    for right in SOURCE_SELECTION_MATRIX_PHRASES
    if left != right
    for selected in (left, right)
]


@pytest.mark.parametrize('left,right,selected', SOURCE_SELECTION_MATRIX_CASES)
def test_source_selection_cartesian_matrix_retains_only_selected_source(
        left, right, selected):
    channel = 'email' if 'messages' in {left, right} else 'Messages'
    prompt = (
        f'send {SOURCE_SELECTION_MATRIX_PHRASES[left]} and '
        f'{SOURCE_SELECTION_MATRIX_PHRASES[right]} to Mom via {channel}, '
        f'but solely the {selected} section')
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == [selected]
    assert set(plan.source_args) == {selected}
    assert not plan.content_error


@pytest.mark.parametrize('prompt,tool,args', [
    ('send my calendar and my messages to Mom via email, '
     'but only the calendar section', 'get_upcoming', {'days': 7}),
    ('send my calendar and an AAPL stock report to Mom via Messages, '
     'solely calendar', 'get_upcoming', {'days': 7}),
    ('send my reminders and my messages to Mom via email, '
     'restricted to reminders', 'search_reminders',
     {'query': '', 'scope': 'all'}),
])
def test_source_selection_uses_only_retained_validator_and_reader(
        tmp_path, monkeypatch, delivery, prompt, tool, args):
    from service import main
    from service.memory import context
    calls = []

    async def selected_read(**kwargs):
        calls.append((tool, kwargs))
        return 'SELECTED_SOURCE: Synthetic permitted content'

    async def excluded_read(**kwargs):
        raise AssertionError('Excluded source was read')

    schemas = {
        'get_upcoming': {'days': {'type': 'integer'}},
        'search_reminders': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}},
        'summarize_messages': {'period': {'type': 'string'}},
        'get_stock_price': {'symbols': {'type': 'array'}},
    }
    for name, properties in schemas.items():
        monkeypatch.setitem(REGISTRY, name, Tool(
            name, 'synthetic', {'properties': properties}, 'assistant_read',
            selected_read if name == tool else excluded_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Selected workflow escaped to fallback routing')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert calls == [(tool, args)]
    assert len(delivery.previews) == 1 and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    store._db.close()


@pytest.mark.parametrize('prompt,calendar_args', [
    ('send my calendar for last week and my reminders due today to Mom '
     'via Messages, only reminders due today', {'period': 'last week'}),
    ('send my reminders due today and my calendar for next week to Mom '
     'via Messages, only reminders due today', {'period': 'next week'}),
])
def test_each_source_owns_its_temporal_range(prompt, calendar_args):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.source_args == {
        'calendar': calendar_args,
        'reminder': {'query': '', 'scope': 'today'},
    }


@pytest.mark.parametrize('prompt,calendar_args', [
    ('send my calendar for last week and my reminders due today to Mom '
     'via Messages, only reminders due today', {'period': 'last week'}),
    ('send my reminders due today and my calendar for next week to Mom '
     'via Messages, only reminders due today', {'period': 'next week'}),
])
def test_source_owned_temporal_ranges_reach_exact_endpoint_calls(
        tmp_path, monkeypatch, delivery, prompt, calendar_args):
    from service import main
    from service.memory import context
    calls = []

    async def calendar_read(**kwargs):
        calls.append(('get_upcoming', kwargs))
        return 'CALENDAR_RANGE: Synthetic matching event'

    async def reminder_read(**kwargs):
        calls.append(('search_reminders', kwargs))
        if kwargs.get('scope') != 'today':
            return 'UNREQUESTED_FUTURE_REMINDER'
        return 'REMINDER_TODAY: Synthetic matching reminder'

    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {
            'days': {'type': 'integer'}, 'period': {'type': 'string'}}},
        'calendar_read', calendar_read))
    monkeypatch.setitem(REGISTRY, 'search_reminders', Tool(
        'search_reminders', 'synthetic', {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'assistant_read', reminder_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Temporal request escaped to fallback routing')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    asyncio.run(agent_events(main, sid, prompt))

    assert dict(calls) == {
        'get_upcoming': calendar_args,
        'search_reminders': {'query': '', 'scope': 'today'},
    }
    assert len(delivery.previews) == 1 and not delivery.effects
    assert 'UNREQUESTED_FUTURE_REMINDER' not in delivery.previews[0]['args']['text']
    store._db.close()


@pytest.mark.parametrize('prompt', [
    ('send my reminders and the calendar section, from Work, '
     'to Mom via Messages'),
    ('send my reminders and the calendar section, incomplete ones, '
     'to Mom via Messages'),
    ('send my reminders and the calendar section, from Work, '
     'to Mom via Messages, only due today'),
])
def test_pre_address_residue_blocks_all_endpoint_reads(
        tmp_path, monkeypatch, delivery, prompt):
    from service import main
    from service.memory import context

    async def forbidden_read(**unused_kwargs):
        raise AssertionError('Unsupported pre-address residue reached a source read')

    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'tripwire', {'properties': {}},
        'calendar_read', forbidden_read))
    monkeypatch.setitem(REGISTRY, 'search_reminders', Tool(
        'search_reminders', 'tripwire', {'properties': {}},
        'assistant_read', forbidden_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Unsupported source residue escaped to fallback routing')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not delivery.reads and not delivery.previews and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    assert store.latest_workflow(sid)['status'] == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('prompt', [
    'send my emails with subject calendar to Mom via Messages',
    'send my emails along with subject news to Mom via Messages',
])
def test_email_subject_qualifier_keywords_never_become_sources(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == ['email']
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt', [
    'send my emails with subject calendar to Mom via Messages',
    'send my emails along with subject news to Mom via Messages',
])
def test_email_subject_qualifier_keywords_block_all_endpoint_reads(
        tmp_path, monkeypatch, delivery, prompt):
    from service import main
    from service.memory import context
    calls = []

    async def tripwire(**kwargs):
        calls.append(kwargs)
        return 'UNRELATED_PAYROLL_EMAIL\nPRIVATE_CALENDAR_SENTINEL'

    for name in ('summarize_emails', 'get_upcoming', 'search_web'):
        monkeypatch.setitem(REGISTRY, name, Tool(
            name, 'tripwire', {'properties': {}}, 'assistant_read', tripwire))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Unsupported subject qualifier escaped to fallback')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not calls and not delivery.previews and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    assert store.latest_workflow(sid)['status'] == 'waiting_for_content'
    store._db.close()


PRE_RECIPIENT_LIMITERS = [
    'only', 'just', 'limited to', 'restricted to',
    'specifically', 'exclusively', 'solely',
]
PRE_RECIPIENT_SOURCE_ORDERS = [
    'my reminders and the calendar section',
    'the calendar section and my reminders',
]


@pytest.mark.parametrize('limiter', PRE_RECIPIENT_LIMITERS)
@pytest.mark.parametrize('payload', PRE_RECIPIENT_SOURCE_ORDERS)
def test_pre_recipient_constraint_preserves_delivery_envelope(limiter, payload):
    plan = compile_new(
        f'send {payload}, {limiter} due today, to Mom via Messages')
    assert plan is not None and plan.status == 'ready'
    assert plan.recipient == 'Mom' and plan.channel == 'messages'
    assert set(plan.sources) == {'calendar', 'reminder'}
    assert plan.source_args == {
        'calendar': {'days': 7},
        'reminder': {'query': '', 'scope': 'today'},
    }


@pytest.mark.parametrize('limiter', PRE_RECIPIENT_LIMITERS)
@pytest.mark.parametrize('payload', PRE_RECIPIENT_SOURCE_ORDERS)
def test_pre_recipient_constraint_reaches_exact_endpoint_reads(
        tmp_path, monkeypatch, delivery, limiter, payload):
    from service import main
    from service.memory import context
    calls = []

    async def calendar_read(**kwargs):
        calls.append(('get_upcoming', kwargs))
        return 'CALENDAR: Synthetic event'

    async def reminder_read(**kwargs):
        calls.append(('search_reminders', kwargs))
        return ('UNREQUESTED_FUTURE_REMINDER' if kwargs.get('scope') != 'today'
                else 'TODAY_REMINDER: Synthetic item')

    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {
            'days': {'type': 'integer'}, 'period': {'type': 'string'}}},
        'calendar_read', calendar_read))
    monkeypatch.setitem(REGISTRY, 'search_reminders', Tool(
        'search_reminders', 'synthetic', {'properties': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}}},
        'assistant_read', reminder_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Pre-recipient constraint escaped to fallback')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    prompt = f'send {payload}, {limiter} due today, to Mom via Messages'
    asyncio.run(agent_events(main, sid, prompt))

    assert dict(calls) == {
        'get_upcoming': {'days': 7},
        'search_reminders': {'query': '', 'scope': 'today'},
    }
    assert len(delivery.previews) == 1 and not delivery.effects
    assert 'UNREQUESTED_FUTURE_REMINDER' not in delivery.previews[0]['args']['text']
    store._db.close()


STOCK_SELECTION_COMPANIONS = {
    'calendar': 'the calendar section',
    'reminder': 'the reminders section',
    'email': 'the email section',
    'messages': 'the messages section',
    'news': 'the news section',
    'weather': 'the weather section',
}
STOCK_SELECTION_CASES = [
    (source, limiter, reverse)
    for source in STOCK_SELECTION_COMPANIONS
    for limiter in PRE_RECIPIENT_LIMITERS
    for reverse in (False, True)
]


@pytest.mark.parametrize('companion,limiter,reverse', STOCK_SELECTION_CASES)
def test_validated_stock_identifier_survives_every_selection_matrix_case(
        companion, limiter, reverse):
    stock = 'an AAPL stock report'
    other = STOCK_SELECTION_COMPANIONS[companion]
    payload = f'{other} and {stock}' if reverse else f'{stock} and {other}'
    channel = 'email' if companion == 'messages' else 'Messages'
    plan = compile_new(
        f'send {payload} to Mom via {channel}, {limiter} stocks')
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == ['stock']
    assert plan.source_args == {'stock': {'symbols': ['AAPL']}}


def test_unvalidated_stock_report_modifier_still_fails_closed():
    plan = compile_new(
        'send my payroll stock report and the calendar section to Mom '
        'via Messages, only stocks')
    assert plan is not None and plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('period,args', [
    ('this week and next week', {'period': 'this week and next week'}),
    ('next two weeks', {'days': 14}),
    ('next 2 weeks', {'days': 14}),
    ('next few weeks', {'days': 21}),
    ('next three weeks', {'days': 21}),
    ('next 3 weeks', {'days': 21}),
    ('past three weeks', {'days': 21}),
    ('last few weeks', {'days': 21}),
    ('next month', {'period': 'next month'}),
    ('this month', {'period': 'this month'}),
])
@pytest.mark.parametrize('after_envelope', [False, True])
def test_complete_calendar_range_grammar_is_calendar_owned(
        period, args, after_envelope):
    prompt = (f'send my calendar to Mom via Messages for {period}'
              if after_envelope
              else f'send my calendar for {period} to Mom via Messages')
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert plan.sources == ['calendar']
    assert plan.source_args == {'calendar': args}


@pytest.mark.parametrize('period,args', [
    ('next three weeks', {'days': 21}),
    ('next few weeks', {'days': 21}),
])
def test_extended_calendar_ranges_reach_exact_endpoint_call(
        tmp_path, monkeypatch, delivery, period, args):
    from service import main
    from service.memory import context
    calls = []

    async def calendar_read(**kwargs):
        calls.append(kwargs)
        return 'CALENDAR_RANGE: Synthetic matching events'

    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool(
        'get_upcoming', 'synthetic', {'properties': {
            'days': {'type': 'integer'}, 'period': {'type': 'string'}}},
        'calendar_read', calendar_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Calendar range escaped to fallback routing')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    asyncio.run(agent_events(
        main, sid, f'send my calendar for {period} to Mom via Messages'))

    assert calls == [args]
    assert len(delivery.previews) == 1 and not delivery.effects
    store._db.close()


EMAIL_QUALIFIER_INTRODUCERS = [
    'with subject',
    'with the subject',
    'with a subject',
    'with subjects',
    'along with subject',
    'along with the subject',
    'with word',
    'with the word',
    'with words',
    'with label',
    'with the label',
    'with labels',
    'with tag',
    'with the tag',
    'with tags',
    'containing',
    'matching',
    'about',
    'whose subject is',
    'whose label is',
    'labeled',
    'labelled',
    'tagged',
    'from',
]
EMAIL_QUALIFIER_SOURCE_VALUES = [
    'calendar', 'news', 'stocks', 'reminders', 'messages',
]
EMAIL_QUALIFIER_MUTATIONS = [
    (noun, introducer, value, ',' if index % 2 else '')
    for noun in ('email', 'emails')
    for index, introducer in enumerate(EMAIL_QUALIFIER_INTRODUCERS)
    for value in EMAIL_QUALIFIER_SOURCE_VALUES
]


@pytest.mark.parametrize(
    'noun,introducer,value,punctuation', EMAIL_QUALIFIER_MUTATIONS)
def test_email_qualifier_mutation_matrix_never_infers_value_as_source(
        noun, introducer, value, punctuation):
    plan = compile_new(
        f'send my {noun}{punctuation} {introducer} {value}{punctuation} '
        'to Mom via Messages')
    assert plan is not None and plan.sources == ['email']
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('value', EMAIL_QUALIFIER_SOURCE_VALUES)
@pytest.mark.parametrize('email_first', [False, True])
@pytest.mark.parametrize('introducer', [
    'with the word', 'with the subject', 'containing', 'whose label is',
])
def test_email_qualifier_with_real_companion_keeps_value_non_source(
        introducer, email_first, value):
    email = f'my emails {introducer} {value}'
    calendar = 'the calendar section'
    payload = f'{email} and {calendar}' if email_first else f'{calendar} and {email}'
    plan = compile_new(f'send {payload} to Mom via Messages')
    assert plan is not None
    assert set(plan.sources) == {'email', 'calendar'}
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('value', EMAIL_QUALIFIER_SOURCE_VALUES)
@pytest.mark.parametrize('email_first', [False, True])
@pytest.mark.parametrize('introducer', [
    'with the word', 'with the subject', 'containing', 'whose label is',
])
def test_email_qualifier_mutations_block_every_endpoint_source(
        tmp_path, monkeypatch, delivery, introducer, email_first, value):
    from service import main
    from service.memory import context
    calls = []

    async def tripwire(**kwargs):
        calls.append(kwargs)
        return 'UNRELATED_PAYROLL_EMAIL\nPRIVATE_SOURCE_SENTINEL'

    for name in (
            'summarize_emails', 'get_upcoming', 'search_web',
            'get_stock_price', 'search_reminders', 'summarize_messages'):
        monkeypatch.setitem(REGISTRY, name, Tool(
            name, 'tripwire', {'properties': {}}, 'assistant_read', tripwire))
    email = f'my emails {introducer} {value}'
    calendar = 'the calendar section'
    payload = f'{email} and {calendar}' if email_first else f'{calendar} and {email}'
    prompt = f'send {payload} to Mom via Messages'
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Unsupported email qualifier escaped to fallback')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not calls and not delivery.previews and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    assert store.latest_workflow(sid)['status'] == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('prompt,expected', [
    ('send my emails and the calendar section to Mom via Messages',
     {'email', 'calendar'}),
    ('send the calendar section and my emails to Mom via Messages',
     {'email', 'calendar'}),
    ('send my emails and the news section to Mom via Messages',
     {'email', 'news'}),
    ('send the news section and my emails to Mom via Messages',
     {'email', 'news'}),
    ('send my emails with my calendar to Mom via Messages',
     {'email', 'calendar'}),
    ('send my emails along with the news section to Mom via Messages',
     {'email', 'news'}),
])
def test_complete_email_source_coordination_remains_supported(prompt, expected):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert set(plan.sources) == expected


@pytest.mark.parametrize('prompt', [
    'send my emails with calendar to Mom via Messages',
    'send my email along with news to Mom via Messages',
])
def test_ambiguous_bare_email_connective_fails_closed(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == ['email']
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


EMAIL_PREQUALIFIER_MODIFIERS = [
    'marked unread', 'summary', 'report', 'digest', 'recap',
]
EMAIL_LATE_QUALIFIER_TEMPLATES = [
    'with subject {value}',
    'with the word {value}',
    'containing {value}',
    '{value} label',
]
EMAIL_SOURCE_NOUN_FORMS = ['my email', 'my emails', 'the inbox']
EMAIL_LATE_QUALIFIER_CASES = [
    (noun, modifier, template, value, ',' if index % 2 else '')
    for noun in EMAIL_SOURCE_NOUN_FORMS
    for modifier in EMAIL_PREQUALIFIER_MODIFIERS
    for index, template in enumerate(EMAIL_LATE_QUALIFIER_TEMPLATES)
    for value in EMAIL_QUALIFIER_SOURCE_VALUES
]


@pytest.mark.parametrize(
    'noun,modifier,template,value,punctuation', EMAIL_LATE_QUALIFIER_CASES)
def test_supported_email_modifier_cannot_hide_later_qualifier(
        noun, modifier, template, value, punctuation):
    qualifier = template.format(value=value)
    plan = compile_new(
        f'send {noun} {modifier}{punctuation} {qualifier}{punctuation} '
        'to Mom via Messages')
    assert plan is not None and plan.sources == ['email']
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


EMAIL_VALUE_FIRST_QUALIFIER_TEMPLATES = [
    'with the {value} label',
    'with a {value} label',
    'with {value} labels',
    '{value} label',
    '{value} subject',
    '{value} tag',
    '{value} word',
    'with the {value} subject',
]
EMAIL_VALUE_FIRST_QUALIFIER_CASES = [
    (noun, template, value, ',' if index % 2 else '')
    for noun in EMAIL_SOURCE_NOUN_FORMS
    for index, template in enumerate(EMAIL_VALUE_FIRST_QUALIFIER_TEMPLATES)
    for value in EMAIL_QUALIFIER_SOURCE_VALUES
]


@pytest.mark.parametrize(
    'noun,template,value,punctuation', EMAIL_VALUE_FIRST_QUALIFIER_CASES)
def test_value_first_email_qualifier_never_prefix_matches_as_source(
        noun, template, value, punctuation):
    qualifier = template.format(value=value)
    plan = compile_new(
        f'send {noun}{punctuation} {qualifier}{punctuation} '
        'to Mom via Messages')
    assert plan is not None and plan.sources == ['email']
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


@pytest.mark.parametrize('prompt', [
    'send my emails marked unread with subject calendar to Mom via Messages',
    'send my emails with the news label to Mom via Messages',
])
def test_intervening_and_value_first_audit_repros_fail_closed(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == ['email']
    assert plan.status == 'waiting_for_content'
    assert plan.content_error and not plan.artifact_text


EMAIL_CLAUSE_ENDPOINT_CASES = [
    (value, email_first, shape)
    for value in EMAIL_QUALIFIER_SOURCE_VALUES
    for email_first in (False, True)
    for shape in ('late', 'value_first')
]


@pytest.mark.parametrize('value,email_first,shape', EMAIL_CLAUSE_ENDPOINT_CASES)
def test_complete_email_clause_qualifiers_block_all_endpoint_reads(
        tmp_path, monkeypatch, delivery, value, email_first, shape):
    from service import main
    from service.memory import context
    calls = []

    async def tripwire(**kwargs):
        calls.append(kwargs)
        return 'UNREAD_PAYROLL_SENTINEL\nPRIVATE_COMPANION_SENTINEL'

    for name in (
            'summarize_emails', 'get_upcoming', 'search_web',
            'get_stock_price', 'search_reminders', 'summarize_messages'):
        monkeypatch.setitem(REGISTRY, name, Tool(
            name, 'tripwire', {'properties': {}}, 'assistant_read', tripwire))
    qualifier = (f'marked unread with subject {value}'
                 if shape == 'late' else f'with the {value} label')
    email = f'my emails {qualifier}'
    calendar = 'the calendar section'
    payload = f'{email} and {calendar}' if email_first else f'{calendar} and {email}'
    prompt = f'send {payload} to Mom via Messages'
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Incomplete email qualifier escaped to fallback')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not calls and not delivery.previews and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    assert store.latest_workflow(sid)['status'] == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('prompt,source', [
    ('send my emails to Mom with subject calendar via Messages', 'email'),
    ('send my emails to Mom via Messages with subject calendar', 'email'),
    ('send my emails to Mom via Messages with the news label', 'email'),
    ('send my emails, to Mom, via Messages, with the word news', 'email'),
    ('send my messages with the word calendar to Mom via email', 'messages'),
    ('send my messages to Mom with the word calendar via email', 'messages'),
    ('send my messages to Mom via email with the news label', 'messages'),
    ('send my messages, to Mom, via email, with subject reminders', 'messages'),
])
def test_private_qualifiers_across_delivery_envelope_fail_closed(prompt, source):
    plan = compile_new(prompt)

    assert plan is not None
    assert plan.status == 'waiting_for_content'
    assert plan.sources == [source]
    assert plan.content_error


@pytest.mark.parametrize('prompt', [
    'send my emails to Mom with subject calendar via Messages',
    'send my emails to Mom via Messages with subject calendar',
    'send my emails to Mom via Messages with the news label',
    'send my emails, to Mom, via Messages, with the word news',
    'send my messages with the word calendar to Mom via email',
    'send my messages to Mom with the word news via email',
    'send my messages to Mom via email with the calendar label',
    'send my messages, to Mom, via email, with subject reminders',
])
def test_private_qualifiers_across_delivery_envelope_block_endpoint_reads(
        tmp_path, monkeypatch, delivery, prompt):
    from service import main
    from service.memory import context
    calls = []

    async def tripwire(**kwargs):
        calls.append(kwargs)
        return 'UNRELATED_PAYROLL_EMAIL\nOUT_OF_FILTER_MESSAGE\nPRIVATE_SOURCE_SENTINEL'

    for name in (
            'summarize_emails', 'get_upcoming', 'search_web',
            'get_stock_price', 'search_reminders', 'summarize_messages'):
        monkeypatch.setitem(REGISTRY, name, Tool(
            name, 'tripwire', {'properties': {}}, 'assistant_read', tripwire))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Unsupported private qualifier escaped to fallback')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert not calls and not delivery.previews and not delivery.effects
    assert not [event for event in events if event.get('type') == 'error']
    assert store.latest_workflow(sid)['status'] == 'waiting_for_content'
    store._db.close()


@pytest.mark.parametrize('prompt,expected', [
    (
        'send my emails with my calendar for tomorrow to Mom via Messages',
        {'summarize_emails': {}, 'get_upcoming': {'period': 'tomorrow'}},
    ),
    (
        'send my calendar for tomorrow along with my emails to Mom via Messages',
        {'summarize_emails': {}, 'get_upcoming': {'period': 'tomorrow'}},
    ),
    (
        'send my emails along with my calendar for next week to Mom via Messages',
        {'summarize_emails': {}, 'get_upcoming': {'period': 'next week'}},
    ),
    (
        'send my calendar for next week along with my emails to Mom via Messages',
        {'summarize_emails': {}, 'get_upcoming': {'period': 'next week'}},
    ),
    (
        'send my emails with my reminders due today to Mom via Messages',
        {'summarize_emails': {},
         'search_reminders': {'query': '', 'scope': 'today'}},
    ),
    (
        'send my reminders due today with my emails to Mom via Messages',
        {'summarize_emails': {},
         'search_reminders': {'query': '', 'scope': 'today'}},
    ),
])
def test_private_source_coordination_keeps_owned_temporal_scope(prompt, expected):
    plan = compile_new(prompt)

    assert plan is not None
    assert plan.status == 'ready'
    decision = compile_decision(plan)
    assert {name: args for name, args in decision.direct_calls
            if name != 'lookup_contact'} == expected


@pytest.mark.parametrize('prompt,expected', [
    (
        'send my emails with my calendar for tomorrow to Mom via Messages',
        {'summarize_emails': {}, 'get_upcoming': {'period': 'tomorrow'}},
    ),
    (
        'send my calendar for tomorrow along with my emails to Mom via Messages',
        {'summarize_emails': {}, 'get_upcoming': {'period': 'tomorrow'}},
    ),
    (
        'send my emails along with my calendar for next week to Mom via Messages',
        {'summarize_emails': {}, 'get_upcoming': {'period': 'next week'}},
    ),
    (
        'send my calendar for next week along with my emails to Mom via Messages',
        {'summarize_emails': {}, 'get_upcoming': {'period': 'next week'}},
    ),
    (
        'send my emails with my reminders due today to Mom via Messages',
        {'summarize_emails': {},
         'search_reminders': {'query': '', 'scope': 'today'}},
    ),
    (
        'send my reminders due today with my emails to Mom via Messages',
        {'summarize_emails': {},
         'search_reminders': {'query': '', 'scope': 'today'}},
    ),
])
def test_private_source_coordination_reaches_exact_endpoint_calls(
        tmp_path, monkeypatch, delivery, prompt, expected):
    from service import main
    from service.memory import context
    calls = []

    properties = {
        'summarize_emails': {},
        'get_upcoming': {
            'days': {'type': 'integer'}, 'period': {'type': 'string'}},
        'search_reminders': {
            'query': {'type': 'string'}, 'scope': {'type': 'string'}},
    }

    for name in expected:
        async def tool_read(_name=name, **kwargs):
            calls.append((_name, kwargs))
            return f'{_name}: SYNTHETIC_MATCHING_RESULT'

        monkeypatch.setitem(REGISTRY, name, Tool(
            name, 'synthetic', {'properties': properties[name]},
            'calendar_read' if name == 'get_upcoming' else 'assistant_read',
            tool_read))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Scoped coordination escaped to fallback routing')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    asyncio.run(agent_events(main, sid, prompt))

    assert dict(calls) == expected
    assert len(delivery.previews) == 1 and not delivery.effects
    store._db.close()


MESSAGE_CONVERSATION_QUALIFIER_CASES = [
    (template.format(qualifier=qualifier.format(value=value)), value)
    for value in ('calendar', 'news')
    for qualifier in (
        'with conversation {value}',
        'with the conversation {value}',
        'with a conversation named {value}',
        'with chat {value}',
        'with a chat named {value}',
    )
    for template in (
        'send my messages {qualifier} to Mom via email',
        'send my messages to Mom {qualifier} via email',
        'send my messages to Mom via email {qualifier}',
    )
]


@pytest.mark.parametrize('prompt,value', MESSAGE_CONVERSATION_QUALIFIER_CASES)
def test_message_conversation_qualifiers_compile_to_exact_filter(prompt, value):
    plan = compile_new(prompt)

    assert plan is not None
    assert plan.status == 'ready'
    assert plan.sources == ['messages']
    assert plan.source_args == {'messages': {'conversation': value}}


@pytest.mark.parametrize('prompt,value', MESSAGE_CONVERSATION_QUALIFIER_CASES)
def test_message_conversation_qualifiers_reach_only_filtered_endpoint_read(
        tmp_path, monkeypatch, delivery, prompt, value):
    from service import main
    from service.memory import context
    calls = []

    async def messages_read(**kwargs):
        calls.append(('summarize_messages', kwargs))
        if kwargs != {'conversation': value}:
            return 'UNRELATED_MESSAGE_SENTINEL'
        return f'MATCHING_{value.upper()}_CONVERSATION'

    async def tripwire(**kwargs):
        calls.append(('unexpected_private_read', kwargs))
        return 'UNRELATED_MESSAGE_SENTINEL\nPRIVATE_COMPANION_SENTINEL'

    monkeypatch.setitem(REGISTRY, 'summarize_messages', Tool(
        'summarize_messages', 'synthetic', {'properties': {
            'conversation': {'type': 'string'}}}, 'assistant_read', messages_read))
    for name in (
            'summarize_emails', 'get_upcoming', 'search_web',
            'get_stock_price', 'search_reminders'):
        monkeypatch.setitem(REGISTRY, name, Tool(
            name, 'tripwire', {'properties': {}}, 'assistant_read', tripwire))
    store, sid = conversation(tmp_path)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(context, 'store', store)
    monkeypatch.setattr(main, 'client', object(), raising=False)
    monkeypatch.setattr(main, 'InteractiveApprover', lambda emit: delivery.approver)

    async def forbidden(*unused_args, **unused_kwargs):
        raise AssertionError('Conversation qualifier escaped to fallback routing')

    monkeypatch.setattr(main, 'route', forbidden)
    monkeypatch.setattr(main, 'ensure_omlx', forbidden)
    events = asyncio.run(agent_events(main, sid, prompt))

    assert calls == [('summarize_messages', {'conversation': value})]
    assert len(delivery.previews) == 1 and not delivery.effects
    assert 'UNRELATED_MESSAGE_SENTINEL' not in str(delivery.previews[0]['args'])
    assert not [event for event in events if event.get('type') == 'error']
    store._db.close()


@pytest.mark.parametrize('prompt,expected_args', [
    ('send my emails marked unread and the calendar section to Mom via Messages',
     {'email': {'unread': True}, 'calendar': {'days': 7}}),
    ('send the calendar section and my emails marked unread to Mom via Messages',
     {'email': {'unread': True}, 'calendar': {'days': 7}}),
    ('send my emails summary with the news section to Mom via Messages',
     {'email': {}, 'news': {'query': 'world news today'}}),
    ('send the news section and my emails report to Mom via Messages',
     {'email': {}, 'news': {'query': 'world news today'}}),
])
def test_complete_coordination_after_email_modifiers_remains_supported(
        prompt, expected_args):
    plan = compile_new(prompt)
    assert plan is not None and plan.status == 'ready'
    assert set(plan.sources) == set(expected_args)
    assert plan.source_args == expected_args
