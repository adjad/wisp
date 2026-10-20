"""Outer speech acts own authority; quoted words and nouns do not create it.

All entrypoint checks use disposable stores, scripted inference, denied
confirmations and inert tools. They establish admission, not model quality.
"""
import copy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import socket
import subprocess
import time

import pytest

from service import utterance_shape as U
from service.memory.store import SessionStore
from service.tasks.compiler import compile_task
from service.workflows.compiler import compile_new, outbound_verb
from service.workflows.engine import prepare_turn
from tests.test_workflow20_knowledge import storage, knowledge_entrypoint


EXPLANATIONS = [
    'Help me understand what "delete all reminders" means.',
    'Could you help me understand the command "delete all reminders"?',
    'Explain the phrase "delete all reminders".',
    'What does "send a message" mean?',
    'What does unread email mean?',
    'Define unread email.',
    'Explain what unread email means.',
    'What is the difference between an email and a message?',
    'Help me understand what ‘delete all reminders’ means.',
    'Hey Wisp, could you help me understand what "delete all reminders" means?',
    'Explain the phrase "delete \\"all\\" reminders".',
    'Could you please define unread email, please?',
]
NON_OUTBOUND = [
    'How can I tell whether an email needs a reply?',
    'What messages did I send yesterday?',
    'What emails did I send yesterday?',
    'What did I send yesterday?',
    'Summarize my unread messages without reading email.',
    'Show me my email and calendar today',
]


@pytest.mark.parametrize('prefix,suffix', [('*', ''), ('**', ''), ('* ', ''), ('**', '**')])
def test_marked_current_source_delivery_keeps_exact_calendar_scope(prefix, suffix):
    prompt = prefix + 'Send Mom a message with my move-in date' + suffix
    assert outbound_verb(prompt)
    plan = compile_new(prompt)
    assert plan is not None and plan.sources == ['calendar']
    assert plan.source_args == {'calendar': {'days': 60, 'calendar_only': True, 'query': 'move in'}}
    assert plan.original_request == prompt and plan.recipient == 'Mom' and plan.channel == 'messages'


@pytest.mark.parametrize('prefix', ['*', '**'])
@pytest.mark.parametrize('prompt', EXPLANATIONS + NON_OUTBOUND)
def test_leading_emphasis_does_not_turn_words_or_history_into_delivery(prefix, prompt):
    assert not outbound_verb(prefix + prompt)
    assert compile_new(prefix + prompt) is None


@pytest.mark.parametrize('prompt', [
    '*Text Mom saying Define unread email.',
    '**Text +15555550123 saying Dr. Alex is late. Define unread email.',
    '***Email alex@example.invalid about Status saying Version 3.14 is ready. Define unread email.',
    '**Text Mom saying "delete all reminders; define unread email".',
])
def test_marked_explicit_literal_bodies_never_become_source_deliveries(prompt):
    assert outbound_verb(prompt)
    assert compile_new(prompt) is None


@pytest.mark.parametrize('prefix', ['_', '__'])
def test_unsupported_word_prefix_cannot_reclassify_source_or_recipient(prefix):
    prompt = prefix + 'Send Mom a message with my move-in date'
    assert not outbound_verb(prompt) and compile_new(prompt) is None
EXPLANATION_READS = [
    'Help me understand what delete all reminders means; then list my reminders.',
    'Define delete all reminders; list my reminders.',
    'List my reminders; help me understand what delete all reminders means.',
    'List my reminders; define delete all reminders.',
    'Define "delete all reminders"; list my reminders.',
    'Define delete all reminders and list my reminders.',
    'List my reminders and define delete all reminders.',
    'Help me understand what delete all reminders means, then list my reminders.',
    'Explain the phrase "delete \\"all; then\\" reminders"; list my reminders.',
]


@pytest.fixture(autouse=True)
def deny_external_effects(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('No network, native process or real inference')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)


@pytest.mark.parametrize('prompt', EXPLANATIONS)
def test_complete_explanation_never_compiles_an_action(prompt):
    assert U.is_complete_explanation(prompt)
    assert compile_task(prompt) is None
    assert compile_new(prompt) is None


@pytest.mark.parametrize('prompt', EXPLANATIONS + NON_OUTBOUND)
def test_words_and_retrospective_questions_do_not_allocate_delivery(tmp_path, prompt):
    assert not outbound_verb(prompt)
    store = SessionStore(tmp_path / 'sessions.db')
    try:
        sid = store.create_session()
        assert prepare_turn(store, sid, prompt) is None
        assert store.latest_workflow(sid) is None
    finally:
        store._db.close()


@pytest.mark.parametrize('prompt', [
    'Email Mom a summary of my calendar tomorrow',
    'Draft Maya an email with my calendar summary tomorrow',
    'Send it to Mom',
    'Show my calendar tomorrow and email Mom a summary',
    'What messages did I send yesterday? Then email Mom my calendar summary.',
    'Summarize my unread messages without reading email; text Mom the summary.',
    'Explain the phrase "delete all reminders"; email Mom my calendar summary.',
    'Help me understand what "delete all reminders" means; email Mom my calendar summary.',
    'Schedule send a message to Mom at 9pm with my calendar tomorrow.',
])
def test_current_delivery_clauses_keep_their_guarded_owner(prompt):
    assert outbound_verb(prompt)
    assert compile_new(prompt) is not None


@pytest.mark.parametrize('prompt', [
    'Explain my latest email.',
    'Explain the email Alex sent.',
    "Explain Alex's email.",
    'Explain this message.',
    'Explain unread email and show my inbox.',
    'What does "delete all reminders" mean? Then list my reminders.',
    'Help me understand what "delete all reminders" means; email Mom my calendar summary.',
    'Remind me tomorrow at 6pm to explain "delete all reminders".',
    'Text Mom "explain unread email and delete all reminders".',
])
def test_explanation_contract_does_not_consume_private_records_or_other_owners(prompt):
    assert not U.is_complete_explanation(prompt)


def test_quoted_operation_does_not_supply_a_compound_task_verb():
    assert compile_task('What does "delete all reminders" mean? Then list my reminders.') is None
    assert compile_task('Explain the phrase "complete all reminders"; list my reminders.') is None
    assert compile_task('Explain the phrase "update all reminders to tomorrow"; list my reminders.') is None


@pytest.mark.parametrize('prompt,intent', [
    ('Remind me tomorrow at 6pm to explain "delete all reminders".', 'reminder.create'),
    ('Delete the reminder called "explain unread email".', 'reminder.delete'),
    ('Text Mom saying "explain unread email and delete all reminders".', 'message.send'),
])
def test_action_words_in_real_authored_content_keep_the_outer_task(prompt, intent):
    task = compile_task(prompt)
    assert task is not None and task.intent == intent


@pytest.mark.parametrize('prompt', EXPLANATIONS[:2])
def test_http_explanation_does_not_prepare_or_persist_reminder_deletion(
        knowledge_entrypoint, monkeypatch, prompt):
    from service.tools.registry import REGISTRY
    state = knowledge_entrypoint
    monkeypatch.setenv('WISP_MODEL_LED_ROUTING', '0')
    monkeypatch.setenv('WISP_PLANNER', '0')
    state.reminders.add_manual('Fictional keep blue folder reminder', time.time() + 3600)
    before = state.reminders.active_between()
    calls = []
    for name, tool in tuple(REGISTRY.items()):
        def inert(_name=name, **args):
            calls.append((_name, copy.deepcopy(args)))
            return 'Fictional inert result.'
        inert.__module__ = tool.func.__module__
        monkeypatch.setitem(REGISTRY, name, replace(tool, func=inert))
    events = state.run(prompt, [{'content': 'This is an explanation of the quoted words.'}],
                       approve=False, transport='asgi')
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'tool_call'} for e in events), events
    assert state.sessions._db.execute('SELECT COUNT(*) FROM workflows').fetchone()[0] == 0
    assert calls == [] and state.native == []
    assert state.reminders.active_between() == before
    assert any(m.get('role') == 'user' and m.get('content') == prompt
               for call in state.clients[0].calls for m in call)


def test_http_real_deletion_still_prepares_exact_selection_and_requires_confirmation(
        knowledge_entrypoint, monkeypatch):
    state = knowledge_entrypoint
    monkeypatch.setenv('WISP_MODEL_LED_ROUTING', '0')
    state.reminders.add_manual('Fictional keep blue folder reminder', time.time() + 3600)
    before = state.reminders.active_between()
    events = state.run('Delete all reminders.', [{'content': 'Synthetic narration.'}],
                       approve=False, transport='asgi')
    confirmations = [e for e in events if e['type'] == 'confirm']
    assert len(confirmations) == 1 and confirmations[0]['tool'] == 'clear_reminders'
    assert confirmations[0]['args'] == {'scope': 'all', 'expected_ids': [r['id'] for r in before]}
    assert any(e['type'] == 'task_plan' for e in events)
    assert state.reminders.active_between() == before and state.native == []


@pytest.fixture
def pending_entrypoint(knowledge_entrypoint, monkeypatch):
    """Actual HTTP modes, direct source seams and every capability stay inert."""
    from service import main
    from service.config.endpoints import Endpoint, Target
    from service.tasks.source_readers import MailReader
    from service.tools.registry import REGISTRY
    from tests import test_direct_dispatch_exec as clients
    state = knowledge_entrypoint
    state.source_calls, state.executed, state.requests, state.raw_sse = [], [], [], []
    from service.agent import loop
    from service.tools.registry import run_tool
    async def execute(tool, args, **kwargs):
        state.tools.append((tool.name, copy.deepcopy(args)))
        return await run_tool(tool, args, **kwargs)
    monkeypatch.setattr(loop, 'run_tool', execute)
    for name, tool in tuple(REGISTRY.items()):
        def inert(_name=name, **args):
            state.executed.append((_name, copy.deepcopy(args)))
            return 'FICTIONAL inert capability result.'
        inert.__module__ = tool.func.__module__
        monkeypatch.setitem(REGISTRY, name, replace(tool, func=inert))
    async def warm(*args, **kwargs):
        state.source_calls.append(('ensure_reply_source', copy.deepcopy(kwargs)))
    def reader(*args, **kwargs):
        state.source_calls.append(('current_mail_reader', copy.deepcopy(kwargs)))
        return MailReader([{'message_id': '<fictional@example.invalid>', 'account': 'Fictional',
            'account_id': 'acct-fictional', 'sender': 'Alex <alex@example.invalid>',
            'subject': 'Blue folder', 'body': 'FICTIONAL body', 'ts': time.time()}],
            accounts=['Fictional'], synced_at=time.time())
    async def preview(args):
        state.source_calls.append(('prepare_reply_args', copy.deepcopy(args)))
        return None, 'Synthetic native preview unavailable; no real compose.'
    monkeypatch.setattr('service.tools.email_tools.ensure_reply_source', warm)
    monkeypatch.setattr('service.tasks.source_readers.current_mail_reader', reader)
    monkeypatch.setattr('service.tools.action_tools.prepare_reply_args', preview)
    target = Target('agent', Endpoint('local', 'http://127.0.0.1:8000', 'local_omlx', True),
                    'Ling-3.0-tiny-Wisp-V2-merged-oQ4e', context_window=32000)
    monkeypatch.setattr(main, 'role_target', lambda role: target)
    monkeypatch.setattr(main, '_model_led_preload', lambda prompt: [])
    original = clients.ScriptedClient
    stream = original.stream_events
    async def close(self):
        pass
    async def capture(self, model, messages, **kwargs):
        state.requests.append(copy.deepcopy({'model': model, 'messages': messages, **kwargs}))
        async for event in stream(self, model, messages, **kwargs):
            yield event
    monkeypatch.setattr(original, 'managed', True, raising=False)
    monkeypatch.setattr(original, 'base_url', target.endpoint.base_url, raising=False)
    monkeypatch.setattr(original, 'endpoint_name', 'local', raising=False)
    monkeypatch.setattr(original, 'aclose', close, raising=False)
    monkeypatch.setattr(original, 'stream_events', capture)
    monkeypatch.setattr(main, 'OMLXClient', lambda **kwargs: original(
        getattr(state, 'owner_script', [{'content': 'Synthetic answer.'}])))
    state.Client = original
    import httpx
    original_post = httpx.AsyncClient.post
    async def post(http, url, **kwargs):
        response = await original_post(http, url, **kwargs)
        if url == '/agent':
            state.raw_sse.append(response.text)
        return response
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    return state


def seed_pending(state, owner):
    from service.tasks.models import SlotValue, TaskPlan, TemporalValue
    from service.workflows.models import WorkflowPlan
    if owner == 'reminder':
        plan = TaskPlan(temporal=TemporalValue(absolute_iso=(
            datetime.now() + timedelta(days=2)).isoformat(timespec='minutes')))
    elif owner.startswith('reply'):
        plan = TaskPlan(kind='task.email.reply', intent='email.reply',
            channel=SlotValue('email', 'explicit'), target=SlotValue('email from Alex', 'explicit'),
            parameters={'reply_all': SlotValue(False)})
        if owner == 'reply-body':
            plan.resolved_references = {'reply.target': {'fields': {
                'message_id': '<fictional@example.invalid>', 'account': 'Fictional',
                'account_id': 'acct-fictional'}}}
        else:
            plan.subject = SlotValue('Thanks.', 'explicit')
    else:
        plan = WorkflowPlan(sources=['email'], recipient='Fictional' if owner == 'workflow-addressed' else '',
                            original_request='Send a summary of my unread email')
    plan.recompute_status()
    sid = state.sessions.create_session()
    state.sessions.save_workflow(sid, plan.to_dict())
    return sid, plan


def pending_rows(state):
    return [tuple(r) for r in state.sessions._db.execute(
        'SELECT id,status,state_json,updated_at FROM workflows ORDER BY id')]


def set_mode(monkeypatch, mode):
    monkeypatch.setenv('WISP_MODEL_LED_ROUTING', '1' if mode == 'model-led' else '0')
    monkeypatch.setenv('WISP_PLANNER', '1' if mode == 'planner' else '0')
    monkeypatch.setenv('WISP_TYPED_REMINDERS_SHADOW_ONLY', '1' if mode == 'shadow' else '0')


@pytest.mark.parametrize('owner', ['reminder', 'reply-body'])
def test_direct_typed_owner_cannot_consume_complete_explanation(pending_entrypoint, owner):
    from service.tasks.engine import prepare_task_turn
    state = pending_entrypoint
    sid, _ = seed_pending(state, owner)
    before = pending_rows(state)
    assert prepare_task_turn(state.sessions, sid, 'Define unread email.',
                             assistant_store=state.reminders) is None
    assert pending_rows(state) == before
    assert not state.source_calls and not state.executed and not state.native


@pytest.mark.parametrize('prompt', EXPLANATIONS)
def test_complete_explanation_has_no_mandatory_or_optional_capabilities(prompt, monkeypatch):
    import asyncio
    from service.router.router import route, routing_guard_contract
    from service.tools.registry import REGISTRY
    from service.workflows.reads import compile_read
    from service.workflows.workflow20 import personal_read
    # A new registration, including an alias to a private tool, must be denied
    # by the current registry envelope without updating a noun/tool denylist.
    monkeypatch.setitem(REGISTRY, 'fictional_private_alias', replace(
        REGISTRY['view_emails'], name='fictional_private_alias'))
    assert personal_read(prompt) is None and compile_read(prompt) is None
    decision = asyncio.run(route(prompt))
    assert not decision.needs_tools and decision.tool_subset == []
    assert not decision.direct_calls and not decision.required_tool_groups
    assert not decision.force_first_tool and not decision.expect_tool_first
    assert not decision.tool_argument_bindings and decision.resolved_request == prompt
    assert set(REGISTRY) <= decision.forbidden_tools
    forbidden, bindings = routing_guard_contract(prompt)
    assert set(REGISTRY) <= forbidden and bindings == {}


def attempt(name, args):
    return {'tool_calls': [{'id': 'fictional-attempt', 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args)}}]}


def actual_attempt(state, monkeypatch, mode, prompt, name, args, *, sid=None):
    from service import main
    set_mode(monkeypatch, mode)
    script = [attempt(name, args), {'content': 'Synthetic answer; no claimed action.'}]
    if mode == 'model-led':
        from service.router.model_led import family_for, registry_specs
        from service.tools.registry import REGISTRY
        spec = next((t for t in registry_specs(REGISTRY) if t.name == name), None)
        family = family_for(spec) if spec is not None else 'other'
        script.insert(0, attempt('get_tool_schemas', {'families': [family], 'tools': [name]}))
    if mode == 'planner':
        script = [{'content': json.dumps({'thought': 'synthetic attempt', 'status': 'call_tools',
                    'calls': [{'tool': name, 'arguments': args}]})},
                  {'content': json.dumps({'thought': 'done', 'status': 'answer', 'calls': []})},
                  {'content': 'Synthetic answer; no claimed action.'}]
    monkeypatch.setattr(main, 'OMLXClient', lambda **kwargs: state.Client(script))
    return state.run(prompt, script, sid=sid, approve=False, transport='asgi')


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt', EXPLANATIONS)
@pytest.mark.parametrize('name,args', [
    ('view_emails', {'unread': True}), ('summarize_emails', {'unread': True}),
    ('view_messages', {}), ('get_upcoming', {}), ('search_reminders', {}),
    ('daily_brief', {}), ('collect_task_evidence', {}),
    ('run_shell', {'command': 'fictional'}), ('run_applescript', {'script': 'fictional'}),
    ('send_message', {'to': 'Fictional', 'text': 'fictional'}),
    ('send_email', {'to': 'fictional@example.invalid', 'subject': 'fictional', 'body': 'fictional'}),
    ('clear_reminders', {'scope': 'all'}), ('http_request', {'url': 'https://fictional.invalid'}),
    ('fictional_private_alias', {}),
])
def test_actual_modes_complete_explanation_rejects_private_and_effect_attempts(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name, args):
    from service.tools.registry import REGISTRY
    state = pending_entrypoint
    monkeypatch.setitem(REGISTRY, 'fictional_private_alias', replace(
        REGISTRY['view_emails'], name='fictional_private_alias'))
    state.reminders.add_manual('Fictional keep blue folder reminder', time.time() + 3600)
    reminders = state.reminders.active_between()
    before = pending_rows(state)
    events = actual_attempt(state, monkeypatch, mode, prompt, name, args)
    record = {'mode': mode, 'prompt': prompt, 'attempt': {'name': name, 'args': args},
        'before': before, 'after': pending_rows(state), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native, 'early': state.early}
    (tmp_path / 'explanation-receipt.json').write_text(json.dumps(record, indent=2, default=str))
    assert not state.executed and not state.source_calls and not state.native
    assert pending_rows(state) == before and state.reminders.active_between() == reminders
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)
    assert not any(e['type'] == 'tool_call' and e.get('name') != 'get_tool_schemas' for e in events)
    assert events[-1]['type'] == 'done'
    routed = next(e for e in events if e['type'] == 'routed')
    assert not routed.get('required_tool_groups')
    if mode == 'model-led':
        assert routed.get('route_source') == 'model_led_discovery'
    if mode == 'planner':
        assert 'planner' in routed.get('reason', '')
    for request in state.requests:
        assert all(tool['function']['name'] == 'get_tool_schemas' for tool in request.get('tools') or [])
    assert any(message.get('role') == 'user' and message.get('content') == prompt
               for request in state.requests for message in request['messages'])


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('owner', ['reminder', 'reply-body', 'reply-target',
                                   'workflow-addressed', 'workflow-unaddressed'])
@pytest.mark.parametrize('name,args', [('view_emails', {'unread': True}),
                                      ('clear_reminders', {'scope': 'all'})])
def test_actual_attempts_cannot_reopen_a_pending_owner_on_complete_explanation(
        pending_entrypoint, monkeypatch, tmp_path, mode, owner, name, args):
    state = pending_entrypoint
    sid, _ = seed_pending(state, owner)
    before = pending_rows(state)
    events = actual_attempt(state, monkeypatch, mode, 'Define unread email.', name, args, sid=sid)
    record = {'mode': mode, 'owner': owner, 'attempt': name, 'before': before,
        'after': pending_rows(state), 'events': events, 'requests': state.requests,
        'raw_sse': state.raw_sse, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native, 'early': state.early}
    (tmp_path / 'pending-attempt-receipt.json').write_text(json.dumps(record, indent=2, default=str))
    assert pending_rows(state) == before
    assert not state.executed and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)
    assert events[-1]['type'] == 'done'


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt', ['Explain my latest email.', 'Explain the email from Alex.'])
def test_actual_specific_private_record_explanation_retains_its_read(
        pending_entrypoint, monkeypatch, mode, prompt):
    state = pending_entrypoint
    assert not U.is_complete_explanation(prompt)
    events = actual_attempt(state, monkeypatch, mode, prompt, 'view_emails', {})
    assert state.executed and {name for name, _ in state.executed} == {'view_emails'}
    assert not state.native and pending_rows(state) == []
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)
    assert events[-1]['type'] == 'done'


@pytest.mark.parametrize('prompt', EXPLANATION_READS)
def test_explanation_clauses_do_not_supply_operation_roles(prompt):
    from service.tasks.compiler import compile_reminder_delete
    assert U.explanation_remainder(prompt) is not None
    assert 'delete' not in U.explanation_remainder(prompt).lower()
    assert compile_task(prompt) is None and compile_reminder_delete(prompt) is None
    assert compile_new(prompt) is None


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt', EXPLANATION_READS)
def test_actual_explanation_with_current_read_cannot_prepare_explained_delete(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt):
    state = pending_entrypoint
    state.reminders.add_manual('Fictional keep blue folder reminder', time.time() + 3600)
    before = state.reminders.active_between()
    events = actual_attempt(state, monkeypatch, mode, prompt, 'clear_reminders', {'scope': 'all'})
    record = {'mode': mode, 'prompt': prompt, 'events': events, 'raw_sse': state.raw_sse,
              'requests': state.requests, 'before': before, 'after': state.reminders.active_between(),
              'plans': pending_rows(state), 'executed': state.executed, 'source_calls': state.source_calls,
              'native': state.native}
    (tmp_path / 'compound-read-receipt.json').write_text(json.dumps(record, indent=2, default=str))
    assert record['after'] == before and pending_rows(state) == []
    assert not state.executed and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)
    assert not any(e['type'] == 'tool_call' and e.get('name') == 'clear_reminders' for e in events)
    assert events[-1]['type'] == 'done'


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
def test_actual_independent_supported_read_still_executes(pending_entrypoint, monkeypatch, mode):
    state = pending_entrypoint
    prompt = 'Define delete all reminders; show my unread email.'
    events = actual_attempt(state, monkeypatch, mode, prompt, 'view_emails', {'unread': False})
    assert state.executed and {name for name, _ in state.executed} == {'view_emails'}
    assert all(args['unread'] is True for _, args in state.executed)
    assert not pending_rows(state) and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('prompt', [
    'Define delete all reminders; delete the Laundry reminder.',
    'Delete the Laundry reminder; define delete all reminders.',
])
def test_genuine_operation_uses_current_clause_target_not_explained_all(prompt):
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    task = compile_task(prompt)
    assert task.intent == 'reminder.delete' and task.target.value == 'Laundry'
    assert task.original_request == prompt
    forbidden, bindings = routing_guard_contract(prompt)
    assert set(REGISTRY) - set(forbidden) == {'clear_reminders'}
    assert bindings == {'clear_reminders': {'scope': 'all', 'query': 'Laundry'}}


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('wide_ids', [False, True])
def test_actual_current_delete_target_cannot_widen_to_explained_all(
        pending_entrypoint, monkeypatch, tmp_path, mode, wide_ids):
    state = pending_entrypoint
    state.reminders.add_manual('Laundry', time.time() + 3600)
    state.reminders.add_manual('Fictional keep blue folder reminder', time.time() + 3600)
    before = state.reminders.active_between()
    prompt = 'Define delete all reminders; delete the Laundry reminder.'
    args = {'scope': 'all', 'query': ''}
    if wide_ids:
        args['expected_ids'] = [r['id'] for r in before]
    events = actual_attempt(state, monkeypatch, mode, prompt, 'clear_reminders', args)
    record = {'mode': mode, 'wide_ids': wide_ids, 'prompt': prompt, 'events': events,
              'raw_sse': state.raw_sse, 'requests': state.requests, 'plans': pending_rows(state),
              'before': before, 'after': state.reminders.active_between(), 'executed': state.executed,
              'native': state.native}
    (tmp_path / 'compound-delete-receipt.json').write_text(json.dumps(record, indent=2, default=str))
    assert state.reminders.active_between() == before and not state.executed and not state.native
    cards = [e for e in events if e['type'] == 'confirm' and e.get('tool') == 'clear_reminders']
    assert len(cards) == 1
    assert 'Laundry' in cards[0].get('preview', '')
    assert 'Fictional keep' not in cards[0].get('preview', '')
    if mode != 'model-led' and mode != 'shadow':
        assert cards[0]['args']['expected_ids'] == [r['id'] for r in before if r['title'] == 'Laundry']
    else:
        assert cards[0]['args']['query'] == 'Laundry'


def test_actual_delete_body_rejects_wide_identity_set_before_retiring(pending_entrypoint, monkeypatch):
    """Actual tool-body identity check, separately from denied HTTP previews."""
    import asyncio
    from service.tools import assistant_tools
    state = pending_entrypoint
    state.reminders.add_manual('Laundry', time.time() + 3600)
    state.reminders.add_manual('Fictional keep blue folder reminder', time.time() + 3600)
    before = state.reminders.active_between()
    retired = []
    async def retire(row):
        retired.append(row)
        return 'Synthetic retirement forbidden'
    monkeypatch.setattr(assistant_tools, '_retire', retire)
    result = asyncio.run(assistant_tools.clear_reminders(
        scope='all', query='Laundry', expected_ids=[r['id'] for r in before]))
    assert 'selected reminder set changed' in result
    assert not retired and not state.native and state.reminders.active_between() == before


@pytest.mark.parametrize('prompt', [
    "Text Mom saying I'll be late. Define unread email.",
    'Text Mom saying "Define unread email; delete all reminders".',
    'Set a reminder tomorrow at 6pm titled Review version 3.14. Define unread email.',
    'Set a reminder tomorrow at 6pm titled “Define unread email; delete all reminders”.',
    'Email alex@example.invalid saying Version 3.14 is ready. Define unread email.',
    'Save a knowledge note titled Project:\nDefine unread email.',
])
def test_authored_owners_keep_definition_sentences_and_punctuation_opaque(prompt):
    assert U.explanation_remainder(prompt) is None


def test_current_authored_operand_bytes_survive_explanation_projection():
    body = 'Text Mom saying Dr. Alex is late. Bring the blue folder.'
    prompt = 'Define unread email; ' + body
    assert U.explanation_remainder(prompt) == body
    quoted = 'Set a reminder tomorrow at 6pm titled “Define unread email; delete all reminders”.'
    assert U.explanation_remainder('Define delete all reminders; ' + quoted) == quoted
    task = compile_task('Define delete all reminders; ' + quoted)
    assert task is not None and task.intent == 'reminder.create'
    assert 'Define unread email; delete all reminders' in task.subject.value


@pytest.mark.parametrize('prompt', [
    'Delete the Laundry reminder.',
    'Set a reminder tomorrow at 6pm titled “Define unread email”.',
    'Text +15555550123 saying Please define unread email.',
])
def test_projected_capability_admission_does_not_mutate_task_plan(prompt):
    from service.tasks.compiler import projected_task_capabilities
    task = compile_task(prompt)
    assert task is not None
    before = copy.deepcopy(task.to_dict())
    assert projected_task_capabilities(prompt, task)
    assert task.to_dict() == before


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('owner', ['reminder', 'reply-body', 'reply-target',
                                   'workflow-addressed', 'workflow-unaddressed'])
def test_explanation_read_compound_preserves_earlier_pending_owner(
        pending_entrypoint, monkeypatch, tmp_path, mode, owner):
    state = pending_entrypoint
    sid, _ = seed_pending(state, owner)
    before = pending_rows(state)
    prompt = 'List my reminders; define delete all reminders.'
    events = actual_attempt(state, monkeypatch, mode, prompt, 'clear_reminders', {'scope': 'all'}, sid=sid)
    (tmp_path / 'compound-pending-receipt.json').write_text(json.dumps({
        'mode': mode, 'owner': owner, 'before': before, 'after': pending_rows(state),
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'executed': state.executed, 'source_calls': state.source_calls, 'native': state.native},
        indent=2, default=str))
    assert pending_rows(state) == before
    assert not state.executed and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('prompt', [
    'Define delete all reminders; delete reminders.',
    'Define delete all reminders; delete the Laundry reminder from the Work account.',
    'Define delete all reminders; delete the Laundry reminder if needed.',
])
def test_unresolved_or_unconsumed_current_delete_has_no_compound_capability(prompt):
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    forbidden, bindings = routing_guard_contract(prompt)
    assert set(REGISTRY) <= forbidden and bindings == {}
    assert compile_task(prompt) is None


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('owner', [None, 'reminder', 'reply-body', 'workflow-addressed'])
@pytest.mark.parametrize('suffix', [
    'delete reminders.',
    'delete the Laundry reminder from the Work account.',
    'delete the Laundry reminder if needed.',
    'complete reminders.',
    'complete the Laundry reminder from the Work account.',
    'complete the Laundry reminder if needed.',
    'update reminders.',
    'update the Laundry reminder to tomorrow from the Work account.',
    'update the Laundry reminder to tomorrow if needed.',
    'Text Mom.',
    'Text +15555550123 at 9pm saying hello.',
    "Reply to Alex's email saying Thanks.",
    'Remind me to buy milk.',
    'Email alex@example.invalid saying hello.',
])
def test_actual_unsupported_projected_operation_has_no_early_or_pending_owner(
        pending_entrypoint, monkeypatch, tmp_path, mode, owner, suffix):
    state = pending_entrypoint
    state.reminders.add_manual('Laundry', time.time() + 3600)
    state.reminders.add_manual('Fictional keep blue folder reminder', time.time() + 3600)
    sid = seed_pending(state, owner)[0] if owner else None
    before, reminders = pending_rows(state), state.reminders.active_between()
    prompt = 'Define delete all reminders; ' + suffix
    events = actual_attempt(state, monkeypatch, mode, prompt, 'clear_reminders',
                            {'scope': 'all', 'query': ''}, sid=sid)
    (tmp_path / 'unsupported-operation-receipt.json').write_text(json.dumps({
        'mode': mode, 'owner': owner, 'prompt': prompt, 'before': before,
        'after': pending_rows(state), 'reminders_before': reminders,
        'reminders_after': state.reminders.active_between(), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert pending_rows(state) == before and state.reminders.active_between() == reminders
    assert not state.executed and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt,intent,tool,change', [
    ('Complete the Laundry reminder.', 'reminder.complete', 'complete_reminder', {}),
    ('Update the Laundry reminder to tomorrow', 'reminder.update', 'update_reminder', {'day': 'tomorrow'}),
])
def test_actual_original_complete_update_actions_keep_identity_and_owner(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, intent, tool, change):
    state = pending_entrypoint
    state.reminders.add_manual('Laundry', time.time() + 3600)
    before = state.reminders.active_between()
    expected = {'title': 'Laundry', 'expected_id': before[0]['id'], **change}
    task = compile_task(prompt)
    assert task and task.intent == intent and not task.missing_slots
    events = actual_attempt(state, monkeypatch, mode, prompt, tool, expected)
    (tmp_path / 'original-operation-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'expected': expected, 'events': events,
        'before': before, 'after': state.reminders.active_between(), 'requests': state.requests,
        'raw_sse': state.raw_sse, 'registry_calls': state.executed,
        'source_calls': state.source_calls, 'native_calls': state.native}, indent=2, default=str))
    owners = [e['task'] for e in events if e['type'] == 'task_plan']
    assert (any(owner['intent'] == intent and owner['target']['value'] == 'Laundry' for owner in owners)
            or any(e['type'] == 'confirm' and e.get('tool') == tool for e in events) or state.executed)
    assert all(name == tool and args['title'] == 'Laundry' for name, args in state.executed)
    assert all(args.get('expected_id', before[0]['id']) == before[0]['id'] for _, args in state.executed)
    if mode in {'normal', 'planner'}:
        assert all(args['expected_id'] == before[0]['id'] for _, args in state.executed)
    assert state.reminders.active_between() == before and not state.source_calls and not state.native


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt', ['Define ephemeral.', 'Explain portfolio theory.'])
@pytest.mark.parametrize('name', ['view_emails', 'fictional_private_alias'])
def test_actual_public_explanation_cannot_acquire_private_capabilities(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name):
    from service.tools.registry import REGISTRY
    from service.router.router import routing_guard_contract
    state = pending_entrypoint
    monkeypatch.setitem(REGISTRY, 'fictional_private_alias', replace(
        REGISTRY['view_emails'], name='fictional_private_alias'))
    forbidden, bindings = routing_guard_contract(prompt)
    assert set(REGISTRY) - forbidden == {'web_search', 'wikipedia_summary'}
    assert bindings == {'web_search': {'query': prompt},
                        'wikipedia_summary': {'topic': 'ephemeral' if 'ephemeral' in prompt else 'portfolio theory'}}
    events = actual_attempt(state, monkeypatch, mode, prompt, name, {})
    (tmp_path / 'public-explanation-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'attempt': name, 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests,
        'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert not pending_rows(state) and not state.source_calls and not state.executed and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt,name,key,value', [
    ('Define ephemeral.', 'wikipedia_summary', 'topic', 'ephemeral'),
    ('Explain portfolio theory.', 'web_search', 'query', 'Explain portfolio theory.'),
])
def test_actual_public_explanation_binds_current_public_request_not_private_history(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name, key, value):
    state = pending_entrypoint
    sid = state.sessions.create_session()
    state.sessions.add_turn(sid, 'user', 'Fictional private history: secret-project-abc.')
    state.sessions.add_turn(sid, 'assistant', 'Fictional private history acknowledged.')
    events = actual_attempt(state, monkeypatch, mode, prompt, name,
                            {key: 'Fictional private history secret-project-abc'}, sid=sid)
    (tmp_path / 'public-request-binding-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'tool': name, 'expected': {key: value},
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    routed = next(e for e in events if e['type'] == 'routed')
    assert routed['tool_argument_bindings'][name] == {key: value}
    assert all(tool == name and args.get(key) == value for tool, args in state.executed)
    for event in events:
        if event['type'] == 'tool_call' and event.get('name') == name:
            assert event['args'][key] == value
    # Optional web remains subject to the existing private-context guard; a
    # denial/no attempt is not evidence of an executed lookup. Never force it.
    if name == 'wikipedia_summary':
        assert state.executed
    assert not pending_rows(state) and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('suffix,supported', [
    ('read my inbox.', True),
    ('read my inbox without reading Mail.', False),
    ('read my inbox from the Work account.', False),
    ('read my inbox; delete all reminders.', False),
])
def test_actual_independent_inbox_read_is_complete_and_source_scoped(
        pending_entrypoint, monkeypatch, tmp_path, mode, suffix, supported):
    state = pending_entrypoint
    prompt = 'Explain "read my inbox"; ' + suffix
    events = actual_attempt(state, monkeypatch, mode, prompt, 'view_emails', {})
    (tmp_path / 'independent-inbox-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'supported': supported, 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests,
        'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert bool(state.executed) == supported
    assert all(name == 'view_emails' for name, _ in state.executed)
    assert not pending_rows(state) and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
def test_current_quoted_literal_send_retains_exact_body_binding(
        pending_entrypoint, monkeypatch, tmp_path, mode):
    state = pending_entrypoint
    body = 'Dr. Alex is late. Define unread email; bring version 3.14.'
    prompt = 'Define delete all reminders; Text +15555550123 saying "' + body + '"'
    task = compile_task(prompt)
    assert task and task.subject.value == body and task.original_request == prompt
    events = actual_attempt(state, monkeypatch, mode, prompt, 'send_message',
                            {'to': '+15555550123', 'text': 'WRONG altered body'})
    (tmp_path / 'compound-send-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'body': body, 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'executed': state.executed,
        'native': state.native}, indent=2, default=str))
    cards = [e for e in events if e['type'] == 'confirm' and e.get('tool') == 'send_message']
    assert len(cards) == 1 and cards[0]['args']['text'] == body
    assert cards[0]['args']['to'] == '+15555550123'
    assert not state.executed and not state.native


@pytest.mark.parametrize('prompt', [
    'Do not read Mail; explain quantum mechanics.',
    'Explain quantum mechanics; do not read Mail.',
])
def test_projection_keeps_explicit_denials_even_without_current_registration(prompt, monkeypatch):
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    monkeypatch.delitem(REGISTRY, 'view_emails', raising=False)
    forbidden, bindings = routing_guard_contract(prompt)
    assert 'view_emails' in forbidden
    assert set(REGISTRY) <= forbidden and not bindings


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
def test_current_unquoted_literal_send_retains_exact_body_binding(
        pending_entrypoint, monkeypatch, tmp_path, mode):
    state = pending_entrypoint
    body = "I'll define unread email for Alex."
    prompt = 'Define delete all reminders; Text +15555550123 saying ' + body
    task = compile_task(prompt)
    assert task and task.subject.value == body and task.original_request == prompt
    events = actual_attempt(state, monkeypatch, mode, prompt, 'send_message',
                            {'to': '+15555550123', 'text': 'WRONG altered body'})
    (tmp_path / 'compound-unquoted-send-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'body': body, 'task': task.to_dict(),
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'executed': state.executed, 'native': state.native}, indent=2, default=str))
    cards = [e for e in events if e['type'] == 'confirm' and e.get('tool') == 'send_message']
    assert len(cards) == 1 and cards[0]['args']['text'] == body
    assert cards[0]['args']['to'] == '+15555550123'
    assert not state.executed and not state.native


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('body', [
    "I'll be late. Define unread email.",
    'Dr. Alex is late. Bring the blue folder.',
])
def test_current_unquoted_multisentence_send_preserves_exact_operand(
        pending_entrypoint, monkeypatch, tmp_path, mode, body):
    state = pending_entrypoint
    authored = 'Text Mom saying ' + body
    task = compile_task(authored)
    assert task and task.subject.value == body
    assert U.explanation_remainder(authored) is None
    prompt = 'Define delete all reminders; Text +15555550123 saying ' + body
    assert U.explanation_remainder(prompt) == 'Text +15555550123 saying ' + body
    projected = compile_task(prompt)
    assert projected and projected.subject.value == body and projected.original_request == prompt
    events = actual_attempt(state, monkeypatch, mode, prompt, 'send_message',
                            {'to': '+15555550123', 'text': 'WRONG altered body'})
    (tmp_path / 'compound-unquoted-multisentence-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'exact_remainder': U.explanation_remainder(prompt),
        'task': task.to_dict(), 'projected_task': projected.to_dict(), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests,
        'executed': state.executed, 'native': state.native}, indent=2, default=str))
    cards = [e for e in events if e['type'] == 'confirm' and e.get('tool') == 'send_message']
    assert len(cards) == 1 and cards[0]['args']['text'] == body
    assert cards[0]['args']['to'] == '+15555550123'
    assert not any(e['type'] == 'workflow' for e in events)
    assert not state.executed and not state.native


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('owner,prompt', [
    ('reminder', 'Define unread email.'),
    ('reminder', 'Help me understand what "delete all reminders" means.'),
    ('reply-body', 'Define unread email.'),
    ('reply-body', 'Explain what unread email means.'),
    ('reply-target', 'Define unread email.'),
    ('workflow-addressed', 'Define unread email.'),
    ('workflow-unaddressed', 'What does unread email mean?'),
])
def test_actual_complete_explanation_preserves_pending_owner_before_source_or_effect(
        pending_entrypoint, monkeypatch, tmp_path, mode, owner, prompt):
    state = pending_entrypoint
    sid, _ = seed_pending(state, owner)
    before = pending_rows(state)
    set_mode(monkeypatch, mode)
    events = state.run(prompt, [{'content': 'Synthetic explanation.'}], sid=sid,
                       approve=False, transport='asgi')
    record = {'mode': mode, 'owner': owner, 'prompt': prompt, 'before': before,
        'after': pending_rows(state), 'events': events, 'raw_sse': state.raw_sse,
        'requests': state.requests, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native, 'early': state.early}
    # Model-led admission and shadow can bypass a legacy owner seam. Record
    # actual early calls rather than claiming every mode invoked that seam.
    (tmp_path / 'pending-receipt.json').write_text(json.dumps(record, indent=2, default=str))
    assert record['after'] == before
    assert state.source_calls == [] and state.executed == [] and state.native == []
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'tool_call', 'error'} for e in events)
    assert events[-1]['type'] == 'done' and state.raw_sse
    routed = next(e for e in events if e['type'] == 'routed')
    if mode == 'model-led':
        assert routed.get('route_source') == 'model_led_discovery'
    if mode == 'planner':
        assert 'planner' in routed.get('reason', '')


@pytest.mark.parametrize('owner,prompt,expected', [
    ('reminder', 'Bring blue folder.', 'add_reminder'),
    ('reminder', '"Define unread email"', 'add_reminder'),
    ('reply-body', 'Thanks for the update.', 'prepare_reply_args'),
    ('reply-body', 'say "Define unread email"', 'prepare_reply_args'),
    ('reply-target', 'try again', 'current_mail_reader'),
    ('workflow-addressed', 'Messages', 'workflow'),
])
def test_genuine_pending_followups_keep_their_original_owner(
        pending_entrypoint, monkeypatch, owner, prompt, expected):
    state = pending_entrypoint
    sid, plan = seed_pending(state, owner)
    set_mode(monkeypatch, 'normal')
    events = state.run(prompt, [{'content': 'Synthetic narration.'}], sid=sid,
                       approve=False, transport='asgi')
    rows = pending_rows(state)
    assert len(rows) == 1 and rows[0][0] == plan.id
    assert json.loads(rows[0][2])['revision'] > plan.revision
    if expected == 'workflow':
        assert any(e['type'] == 'workflow' and e['event'] == 'execution_started' for e in events)
    else:
        assert expected in [name for name, _ in state.executed + state.source_calls]
    assert not state.native


HISTORY_QUESTIONS = [
    'What messages did I send yesterday?',
    '*What messages did I send yesterday?',
    '**What messages did I send yesterday?',
    '**What messages did I send yesterday?**',
    'What emails did I send yesterday?',
    '*What emails did I write today?',
    'What did I send yesterday?',
]


@pytest.mark.parametrize('prompt', HISTORY_QUESTIONS)
def test_whole_history_question_only_subtracts_from_existing_source_menu(prompt, monkeypatch):
    import asyncio
    from service.router import router
    from service.tools.registry import REGISTRY
    monkeypatch.setitem(REGISTRY, 'fictional_unknown_read_alias', replace(
        REGISTRY['view_messages'], name='fictional_unknown_read_alias'))
    legacy = router._domain_subset(prompt)
    reads = {'view_messages', 'summarize_messages', 'view_emails', 'summarize_emails'}
    expected = set(legacy.tool_subset or ()) & reads if legacy else set()
    assert U.is_retrospective_communication_question(prompt)
    assert not outbound_verb(prompt) and compile_new(prompt) is None and compile_task(prompt) is None
    forbidden, bindings = router.routing_guard_contract(prompt)
    decision = asyncio.run(router.route(prompt))
    assert set(REGISTRY) - set(forbidden) == expected == set(decision.tool_subset)
    assert set(REGISTRY) - expected <= decision.forbidden_tools
    assert not bindings and not decision.tool_argument_bindings
    assert not decision.expect_tool_first and not decision.direct_calls and not decision.required_tool_groups
    assert decision.resolved_request == prompt
    if 'emails' in prompt:
        assert expected <= {'view_emails', 'summarize_emails'}
    elif 'messages' in prompt:
        assert expected <= {'view_messages', 'summarize_messages'}


@pytest.mark.parametrize('prompt', [
    'What messages did I send yesterday? Then delete the Laundry reminder.',
    'What messages did I send yesterday? Text +15555550123 saying "Keep blue folder."',
    'Text +15555550123 saying What messages did I send yesterday?',
    'Text +15555550123 saying "*What messages did I send yesterday?"',
    'Remind me tomorrow to ask what messages did I send yesterday.',
    '"What messages did I send yesterday?"',
    'say "What messages did I send yesterday?"',
    'Delete the Laundry reminder.',
])
def test_history_proof_does_not_consume_current_clause_or_literal_operand(prompt):
    assert not U.is_retrospective_communication_question(prompt)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt', HISTORY_QUESTIONS[:3])
@pytest.mark.parametrize('name,args', [
    ('send_message', {'to': '+15555550123', 'text': 'Unrequested synthetic action'}),
    ('draft_message', {'to': '+15555550123', 'text': 'Unrequested synthetic draft'}),
    ('clear_reminders', {'scope': 'all'}),
    ('run_shell', {'command': 'fictional'}),
    ('run_applescript', {'script': 'fictional'}),
    ('fictional_action_alias', {'to': '+15555550123', 'text': 'fictional'}),
    ('fictional_unknown_read_alias', {}),
    ('view_emails', {}),
])
def test_actual_history_question_rejects_effects_aliases_and_other_source(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name, args):
    from service.tools.registry import REGISTRY
    state = pending_entrypoint
    monkeypatch.setitem(REGISTRY, 'fictional_action_alias', replace(
        REGISTRY['send_message'], name='fictional_action_alias'))
    monkeypatch.setitem(REGISTRY, 'fictional_unknown_read_alias', replace(
        REGISTRY['view_messages'], name='fictional_unknown_read_alias'))
    state.reminders.add_manual('Fictional keep blue folder reminder', time.time() + 3600)
    reminders = state.reminders.active_between()
    before = pending_rows(state)
    events = actual_attempt(state, monkeypatch, mode, prompt, name, args)
    record = {'mode': mode, 'prompt': prompt, 'attempt': {'name': name, 'args': args},
        'before': before, 'after': pending_rows(state), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native, 'early': state.early}
    (tmp_path / 'history-receipt.json').write_text(json.dumps(record, indent=2, default=str))
    assert not state.executed and not state.source_calls and not state.native
    assert pending_rows(state) == before and state.reminders.active_between() == reminders
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)
    assert not any(e['type'] == 'tool_call' and e.get('name') != 'get_tool_schemas' for e in events)
    assert events[-1]['type'] == 'done'
    assert any(m.get('role') == 'user' and m.get('content') == prompt
               for request in state.requests for m in request['messages'])


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt,name', [
    ('*What messages did I send yesterday?', 'view_messages'),
    ('**What emails did I send yesterday?', 'view_emails'),
])
def test_actual_history_question_preserves_existing_domain_read(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name):
    state = pending_entrypoint
    events = actual_attempt(state, monkeypatch, mode, prompt, name, {})
    (tmp_path / 'history-read-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'events': events, 'raw_sse': state.raw_sse,
        'requests': state.requests, 'registry_calls': state.executed,
        'source_calls': state.source_calls, 'native_calls': state.native,
        'plans': pending_rows(state)}, indent=2, default=str))
    assert state.executed and all(n == name for n, _ in state.executed)
    assert not pending_rows(state) and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('owner', ['reminder', 'reply-body', 'reply-target',
                                   'workflow-addressed', 'workflow-unaddressed'])
def test_actual_whole_history_question_preserves_pending_owner_before_warming(
        pending_entrypoint, monkeypatch, tmp_path, mode, owner):
    state = pending_entrypoint
    sid, _ = seed_pending(state, owner)
    before = pending_rows(state)
    prompt = '*What messages did I send yesterday?'
    events = actual_attempt(state, monkeypatch, mode, prompt, 'send_message',
                            {'to': '+15555550123', 'text': 'Unrequested synthetic action'}, sid=sid)
    record = {'mode': mode, 'owner': owner, 'prompt': prompt, 'before': before,
        'after': pending_rows(state), 'events': events, 'raw_sse': state.raw_sse,
        'requests': state.requests, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native, 'early': state.early}
    (tmp_path / 'pending-history-receipt.json').write_text(json.dumps(record, indent=2, default=str))
    assert record['after'] == before
    assert not state.source_calls and not state.executed and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('quoted', [False, True])
def test_actual_current_literal_history_body_retains_exact_operand(
        pending_entrypoint, monkeypatch, tmp_path, mode, quoted):
    state = pending_entrypoint
    body = '*What messages did I send yesterday?'
    prompt = 'Text +15555550123 saying ' + ('"' + body + '"' if quoted else body)
    task = compile_task(prompt)
    assert task and task.subject.value == body and task.original_request == prompt
    events = actual_attempt(state, monkeypatch, mode, prompt, 'send_message',
                            {'to': '+15555550123', 'text': body})
    (tmp_path / 'literal-history-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'task': task.to_dict(), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'registry_calls': state.executed,
        'source_calls': state.source_calls, 'native_calls': state.native}, indent=2, default=str))
    cards = [e for e in events if e['type'] == 'confirm' and e.get('tool') == 'send_message']
    assert len(cards) == 1 and cards[0]['args']['text'] == body
    assert cards[0]['args']['to'] == '+15555550123'
    assert not state.executed and not state.source_calls and not state.native


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prefix', ['', 'What messages did I send yesterday? '])
def test_actual_independent_current_delete_keeps_existing_owner(
        pending_entrypoint, monkeypatch, tmp_path, mode, prefix):
    state = pending_entrypoint
    prompt = prefix + 'Delete the Laundry reminder.'
    task = compile_task(prompt)
    assert task and task.intent == 'reminder.delete' and task.target.value == 'Laundry'
    assert not U.is_retrospective_communication_question(prompt)
    state.reminders.add_manual('Laundry', time.time() + 3600)
    state.reminders.add_manual('Fictional keep blue folder reminder', time.time() + 3600)
    before = state.reminders.active_between()
    events = actual_attempt(state, monkeypatch, mode, prompt, 'clear_reminders',
                            {'scope': 'all', 'query': 'Laundry'})
    (tmp_path / 'history-independent-delete-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'task': task.to_dict(), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'before': before,
        'after': state.reminders.active_between(), 'plans': pending_rows(state),
        'registry_calls': state.executed, 'source_calls': state.source_calls,
        'native_calls': state.native}, indent=2, default=str))
    cards = [e for e in events if e['type'] == 'confirm' and e.get('tool') == 'clear_reminders']
    if mode == 'shadow' and prefix:
        # Existing compound routing requires the historical read first. A
        # script that skips it may retain the exact typed owner but cannot
        # preview the delete. This is also observed on immutable b415.
        tasks = [e['task'] for e in events if e['type'] == 'task_plan']
        assert tasks and tasks[0]['target']['value'] == 'Laundry'
        assert not cards
        assert any(e['type'] == 'tool_result' and 'required source steps' in e.get('result', '')
                   for e in events)
    else:
        assert len(cards) == 1 and 'Laundry' in cards[0].get('preview', '')
        assert 'Fictional keep' not in cards[0].get('preview', '')
    assert state.reminders.active_between() == before
    assert not state.executed and not state.source_calls and not state.native


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('marker', ['*', '**'])
def test_actual_marked_direct_phone_source_delivery_keeps_exact_existing_bindings(
        pending_entrypoint, monkeypatch, tmp_path, mode, marker):
    from service.workflows.compiler import compile_decision
    state = pending_entrypoint
    prompt = marker + 'Send +15555550123 a message with my move-in date'
    expected = {'days': 60, 'calendar_only': True, 'query': 'move in'}
    plan = compile_new(prompt)
    assert plan and plan.recipient == '+15555550123'
    assert plan.source_args == {'calendar': expected} and plan.original_request == prompt
    decision = compile_decision(plan)
    assert decision.tool_argument_bindings['get_upcoming'] == expected
    assert decision.tool_argument_bindings['send_message'] == {'to': '+15555550123'}
    events = actual_attempt(state, monkeypatch, mode, prompt, 'get_upcoming', expected)
    (tmp_path / 'marked-phone-source-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'plan': plan.to_dict(), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'plans': pending_rows(state),
        'registry_calls': state.executed, 'source_calls': state.source_calls,
        'native_calls': state.native}, indent=2, default=str))
    assert state.executed and all(n == 'get_upcoming' and args == expected for n, args in state.executed)
    assert not state.source_calls and not state.native
    assert not any(e['type'] == 'error' for e in events)
    for event in events:
        if event['type'] == 'workflow' and 'workflow' in event:
            assert event['workflow']['source_args'] == {'calendar': expected}
            assert event['workflow']['recipient'] == '+15555550123'
        if event['type'] == 'confirm':
            assert event['tool'] == 'send_message' and event['args']['to'] == '+15555550123'


OLD_HISTORY_QUESTIONS = [
    'Did Mom email me?',
    'What did Mom email me?',
    'What did Alex text me yesterday?',
    'Did I send yesterday?',
    'What has Alex said?',
    'What had my brother told me?',
    'Did my father write to me?',
    'What did Adi e-mail me earlier?',
]


@pytest.mark.parametrize('marker', ['', '*', '**'])
@pytest.mark.parametrize('verb', [
    'text', 'texted', 'email', 'emailed', 'e-mail', 'e-mailed',
    'message', 'messaged', 'messages', 'send', 'sent', 'write', 'wrote',
    'say', 'said', 'tell', 'told',
])
def test_existing_retrospective_verb_family_has_one_shared_full_question_proof(marker, verb):
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    prompt = marker + 'What did Alex ' + verb + ' me yesterday?'
    assert U.is_retrospective_communication_question(prompt)
    assert not outbound_verb(prompt) and compile_new(prompt) is None
    forbidden, bindings = routing_guard_contract(prompt)
    reads = {'view_messages', 'summarize_messages', 'view_emails', 'summarize_emails'}
    assert set(REGISTRY) - reads <= forbidden and not bindings
    # Neither the old family nor the new family can consume a later command.
    assert not U.is_retrospective_communication_question(prompt + ' Text Mom saying "Bring blue folder."')
    assert not U.is_retrospective_communication_question('Text Mom saying "' + prompt + '"')


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt', OLD_HISTORY_QUESTIONS)
@pytest.mark.parametrize('name,args', [
    ('send_message', {'to': '+15555550123', 'text': 'Unrequested synthetic action'}),
    ('send_email', {'to': 'fictional@example.invalid', 'subject': 'fictional', 'body': 'Unrequested synthetic action'}),
    ('fictional_action_alias', {'to': '+15555550123', 'text': 'fictional'}),
    ('run_shell', {'command': 'fictional'}),
    ('web_search', {'query': 'Unrequested disclosure of private history'}),
])
def test_actual_existing_history_families_reject_all_outbound_and_opaque_attempts(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name, args):
    from service.tools.registry import REGISTRY
    state = pending_entrypoint
    monkeypatch.setitem(REGISTRY, 'fictional_action_alias', replace(
        REGISTRY['send_message'], name='fictional_action_alias'))
    assert U.is_retrospective_communication_question(prompt)
    before = pending_rows(state)
    events = actual_attempt(state, monkeypatch, mode, prompt, name, args)
    (tmp_path / 'sibling-history-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'attempt': {'name': name, 'args': args},
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'before': before, 'after': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native, 'early': state.early}, indent=2, default=str))
    assert not state.executed and not state.source_calls and not state.native
    assert pending_rows(state) == before
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)
    assert not any(e['type'] == 'tool_call' and e.get('name') != 'get_tool_schemas' for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt,name', [
    ('Did Mom email me?', 'view_emails'),
    ('What did Mom email me?', 'view_emails'),
    ('What did Alex text me yesterday?', 'view_messages'),
])
def test_actual_existing_sibling_history_retains_only_inherited_source_read(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name):
    from service.router.router import _domain_subset
    state = pending_entrypoint
    legacy = _domain_subset(prompt)
    expected = bool(legacy and name in (legacy.tool_subset or ()))
    events = actual_attempt(state, monkeypatch, mode, prompt, name, {})
    (tmp_path / 'sibling-history-read-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'expected_existing_menu': expected,
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert bool(state.executed) == expected and all(n == name for n, _ in state.executed)
    assert not pending_rows(state) and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('owner', ['reminder', 'reply-body', 'reply-target',
                                   'workflow-addressed', 'workflow-unaddressed'])
def test_actual_existing_history_sibling_preserves_pending_rows_before_warming(
        pending_entrypoint, monkeypatch, tmp_path, mode, owner):
    state = pending_entrypoint
    sid, _ = seed_pending(state, owner)
    before = pending_rows(state)
    prompt = '*What did Alex text me yesterday?'
    events = actual_attempt(state, monkeypatch, mode, prompt, 'send_message',
                            {'to': '+15555550123', 'text': 'Unrequested synthetic action'}, sid=sid)
    (tmp_path / 'pending-sibling-history-receipt.json').write_text(json.dumps({
        'mode': mode, 'owner': owner, 'prompt': prompt, 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'before': before,
        'after': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native, 'early': state.early}, indent=2, default=str))
    assert pending_rows(state) == before
    assert not state.source_calls and not state.executed and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('body', ['What did Alex text me yesterday?', '*Did I send yesterday?'])
def test_actual_existing_history_sibling_is_literal_in_authored_body(
        pending_entrypoint, monkeypatch, tmp_path, mode, body):
    state = pending_entrypoint
    prompt = 'Text +15555550123 saying "' + body + '"'
    task = compile_task(prompt)
    assert task and task.subject.value == body and task.original_request == prompt
    events = actual_attempt(state, monkeypatch, mode, prompt, 'send_message',
                            {'to': '+15555550123', 'text': body})
    (tmp_path / 'literal-sibling-history-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'task': task.to_dict(), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    cards = [e for e in events if e['type'] == 'confirm' and e.get('tool') == 'send_message']
    assert len(cards) == 1 and cards[0]['args']['text'] == body
    assert cards[0]['args']['to'] == '+15555550123'
    assert not state.executed and not state.source_calls and not state.native


RELATIVE_HISTORY_QUESTIONS = [
    'What messages did I send last week?',
    'What emails did I send to Alex yesterday?',
    '*What messages did I write last month?',
    '**What emails did I send last year?**',
    'What messages did I send to Alex Smith yesterday?',
    'What messages did I send to Alex Lee Smith last week?',
    'What emails did I send to fictional@example.invalid yesterday?',
    'What messages did I send to +15555550123 yesterday?',
]


@pytest.mark.parametrize('prompt', RELATIVE_HISTORY_QUESTIONS)
def test_finite_relative_history_preserves_no_effect_contract(prompt):
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    assert U.is_retrospective_communication_question(prompt)
    assert not outbound_verb(prompt) and compile_new(prompt) is None
    forbidden, bindings = routing_guard_contract(prompt)
    assert set(REGISTRY) - {'view_messages', 'summarize_messages', 'view_emails', 'summarize_emails'} <= forbidden
    assert not bindings


@pytest.mark.parametrize('prompt', [
    'What emails did I send to Alex and Send Mom a message?',
    'What emails did I send to Alex Then Text Mom saying "Bring blue folder."',
    'What emails did I send to Alex; Send Mom a message.',
    'What messages did I send last week? Then Text Mom saying Bring version 3.14.',
    'What messages did I send last week and Send Mom a message.',
    'What messages did I send last week? Schedule a text to Mom tomorrow.',
    'What messages did I send to Alex yesterday? Delete the Laundry reminder.',
    'Text Mom saying What messages did I send last week?',
    'Text Mom saying "What emails did I send to Alex yesterday?"',
    'Remind me tomorrow to ask what messages did I send last week.',
    'What messages did I send during the previous fortnight?',
    'What emails did I send to Alex Smith Lee Jones yesterday?',
])
def test_finite_history_does_not_absorb_current_clauses_data_or_unknown_temporal_grammar(prompt):
    assert not U.is_retrospective_communication_question(prompt)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt', RELATIVE_HISTORY_QUESTIONS[:2])
@pytest.mark.parametrize('name,args', [
    ('send_message', {'to': '+15555550123', 'text': 'Unrequested synthetic action'}),
    ('send_email', {'to': 'fictional@example.invalid', 'subject': 'fictional', 'body': 'Unrequested synthetic action'}),
    ('fictional_unknown_read_alias', {}),
    ('run_applescript', {'script': 'fictional'}),
])
def test_actual_relative_history_rejects_outbound_alias_and_opaque_attempts(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name, args):
    from service.tools.registry import REGISTRY
    state = pending_entrypoint
    monkeypatch.setitem(REGISTRY, 'fictional_unknown_read_alias', replace(
        REGISTRY['view_messages'], name='fictional_unknown_read_alias'))
    events = actual_attempt(state, monkeypatch, mode, prompt, name, args)
    (tmp_path / 'relative-history-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'attempt': {'name': name, 'args': args},
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert not state.executed and not state.source_calls and not state.native
    assert not pending_rows(state)
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)
    assert not any(e['type'] == 'tool_call' and e.get('name') != 'get_tool_schemas' for e in events)


@pytest.mark.parametrize('prompt,private,speaker', [
    ('what did I say today', True, 'I'),
    ('What messages did I send last week?', True, 'I'),
    ('What did Inez say today?', False, 'Inez'),
    ('what did Trump say today', False, 'Trump'),
    ('What did Alex text yesterday?', True, 'Alex'),
    ('What did Alex email me yesterday?', True, 'Alex'),
    ('What did Alex say to me yesterday?', True, 'Alex'),
    ('What did my doctor say yesterday?', True, 'my doctor'),
])
def test_historical_shape_exposes_roles_without_inventing_private_source(prompt, private, speaker):
    assert U.retrospective_communication_roles(prompt) == (private, speaker)
    assert U.is_retrospective_communication_question(prompt)


@pytest.mark.parametrize('prompt', ['what did Trump say today', '*What did Avery Cole say yesterday?'])
def test_unowned_history_preserves_inherited_positive_owner_and_classifier_count(
        pending_entrypoint, monkeypatch, tmp_path, prompt):
    import asyncio
    from service.router import router
    from service.tools import imessage_tools
    monkeypatch.setattr(imessage_tools, '_contacts', {'+15550001': 'Trisha Jain', '+15550002': 'Dan Smith'})
    original = router._classify_web_request
    calls = []
    def classify(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(router, '_classify_web_request', classify)
    with monkeypatch.context() as inherited:
        # Component comparison isolates the new history overlay. The full
        # inherited route body/classifier stays unchanged and never executes.
        inherited.setattr(router, '_history_read_contract', lambda text: None)
        inherited.setattr(router, 'is_retrospective_communication_question', lambda text: False)
        baseline = asyncio.run(router.route(prompt))
    baseline_count = len(calls)
    calls.clear()
    current = asyncio.run(router.route(prompt))
    assert len(calls) == baseline_count
    assert current.tool_subset == baseline.tool_subset
    if prompt == 'what did Trump say today':
        assert current.tool_subset == ['web_search']
    for field in ('direct_calls', 'tool_argument_bindings', 'required_tool_groups',
                  'force_first_tool', 'expect_tool_first', 'resolved_request',
                  'isolated_public_query', 'verified_results_only', 'strict_read_limits'):
        assert getattr(current, field) == getattr(baseline, field), field
    assert {'send_message', 'send_email'} <= current.forbidden_tools
    if 'web_search' in current.tool_subset:
        assert {'view_messages', 'summarize_messages', 'view_emails', 'summarize_emails'} <= current.forbidden_tools
    (tmp_path / 'public-history-owner-component.json').write_text(json.dumps({
        'prompt': prompt, 'baseline': baseline.as_dict(), 'current': current.as_dict(),
        'baseline_classifier_count': baseline_count, 'current_classifier_count': len(calls),
        'evidence': 'component inherited-owner comparison; no HTTP or capability execution'}, indent=2, default=str))


@pytest.mark.parametrize('prompt', [
    'what did trishe say', 'what did my mom say yesterday',
    'what did trishe say yesterday', 'what did Dan say today',
    'Did Dan say today?', 'What did Trump say to me today?',
    'What messages did I send last week?',
])
def test_private_or_inherited_untimed_history_keeps_only_existing_private_reads(
        pending_entrypoint, monkeypatch, prompt):
    import asyncio
    from service.router import router
    from service.tools import imessage_tools
    from service.tools.registry import REGISTRY
    monkeypatch.setattr(imessage_tools, '_contacts', {'+15550001': 'Trisha Jain', '+15550002': 'Dan Smith'})
    decision = asyncio.run(router.route(prompt))
    expected = {'view_messages', 'summarize_messages', 'view_emails', 'summarize_emails'}
    # A known contact cannot invent a missing source owner: e.g. the existing
    # scoped menu for "Did Dan say today?" is absent, so it clarifies closed.
    assert set(decision.tool_subset or ()) <= expected
    assert 'web_search' in decision.forbidden_tools
    forbidden, bindings = router.routing_guard_contract(prompt)
    assert set(REGISTRY) - set(forbidden) <= expected and not bindings


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('name,args', [
    ('send_message', {'to': '+15555550123', 'text': 'Unrequested synthetic action'}),
    ('send_email', {'to': 'fictional@example.invalid', 'subject': 'fictional', 'body': 'Unrequested synthetic action'}),
    ('view_messages', {}), ('view_emails', {}),
    ('fictional_unknown_read_alias', {}), ('run_shell', {'command': 'fictional'}),
])
def test_actual_unknown_public_historical_speaker_cannot_acquire_private_or_effect_capability(
        pending_entrypoint, monkeypatch, tmp_path, mode, name, args):
    from service.tools import imessage_tools
    from service.tools.registry import REGISTRY
    state = pending_entrypoint
    monkeypatch.setattr(imessage_tools, '_contacts', {'+15550001': 'Trisha Jain', '+15550002': 'Dan Smith'})
    monkeypatch.setitem(REGISTRY, 'fictional_unknown_read_alias', replace(
        REGISTRY['view_messages'], name='fictional_unknown_read_alias'))
    prompt = 'what did Trump say today'
    events = actual_public_history_attempt(state, monkeypatch, mode, prompt, name, args)
    (tmp_path / 'public-history-negative-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'attempt': {'name': name, 'args': args},
        'protocol': 'public web compatibility tool calls (including WISP_PLANNER=1); model-led discovery when selected',
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert all(n == 'web_search' and a == {'query': prompt} for n, a in state.executed)
    assert not pending_rows(state) and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('private_context', [False, True])
def test_actual_unknown_public_history_web_uses_existing_current_query_not_private_history(
        pending_entrypoint, monkeypatch, tmp_path, mode, private_context):
    from service.tools import imessage_tools
    state = pending_entrypoint
    monkeypatch.setattr(imessage_tools, '_contacts', {'+15550001': 'Trisha Jain', '+15550002': 'Dan Smith'})
    sid = state.sessions.create_session()
    if private_context:
        state.sessions.add_turn(sid, 'user', 'Fictional private history: secret-project-abc.')
        state.sessions.add_turn(sid, 'assistant', 'Fictional private history acknowledged.')
    prompt = 'what did Trump say today'
    events = actual_public_history_attempt(state, monkeypatch, mode, prompt, 'web_search',
                            {'query': 'Fictional private history secret-project-abc' if private_context else prompt}, sid=sid)
    (tmp_path / 'public-history-web-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'private_context': private_context,
        'protocol': 'public web compatibility tool calls (including WISP_PLANNER=1); model-led discovery when selected',
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert all(n == 'web_search' and args == {'query': prompt} for n, args in state.executed)
    # Optional discovery remains subject to the inherited private-context
    # egress guard. No attempted/denied read is claimed as an executed one.
    if mode == 'planner':
        # The inherited planner menu omits public_web for this wording. Its
        # empty safe intersection denies the attempt; do not fabricate a
        # successful read or expand menu ownership to make a control pass.
        assert not state.executed
        assert all(tool['function']['name'] != 'web_search'
                   for request in state.requests for tool in request.get('tools') or [])
        assert any(e['type'] == 'tool_result' and 'not available' in e.get('result', '') for e in events)
    elif not private_context:
        assert state.executed
    assert not pending_rows(state) and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt,name', [
    ('what did trishe say', 'view_messages'),
    ('what did Dan say today', 'view_messages'),
    ('what did trishe say yesterday', 'view_messages'),
    ('What did Trump say to me today?', 'view_messages'),
])
def test_actual_untimed_known_and_explicit_private_history_preserves_read_owner(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name):
    from service.tools import imessage_tools
    state = pending_entrypoint
    monkeypatch.setattr(imessage_tools, '_contacts', {'+15550001': 'Trisha Jain', '+15550002': 'Dan Smith'})
    events = actual_attempt(state, monkeypatch, mode, prompt, name, {})
    (tmp_path / 'private-history-read-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'events': events, 'raw_sse': state.raw_sse,
        'requests': state.requests, 'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert state.executed and all(n == name for n, _ in state.executed)
    assert not pending_rows(state) and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('prompt,contacts,expected', [
    ('what did trishe say', {}, {'view_messages', 'summarize_messages', 'view_emails', 'summarize_emails'}),
    ('what did trishe say yesterday', {'+15550001': 'Trisha Jain'},
     {'view_messages', 'summarize_messages', 'view_emails', 'summarize_emails'}),
    ('what did Dan say today', {'+15550002': 'Dan Smith'},
     {'view_messages', 'summarize_messages', 'view_emails', 'summarize_emails'}),
    ('what did Trump say today', {}, {'web_search'}),
    ('what did Trump say today', {'+15550001': 'Trisha Jain', '+15550002': 'Dan Smith'}, {'web_search'}),
])
def test_existing_historical_source_owner_uses_contact_and_public_evidence_not_name_special_case(
        pending_entrypoint, monkeypatch, prompt, contacts, expected):
    import asyncio
    from service.router import router
    from service.tools import imessage_tools
    from service.tools.registry import REGISTRY
    monkeypatch.setattr(imessage_tools, '_contacts', contacts)
    decision = asyncio.run(router.route(prompt))
    assert set(decision.tool_subset) == expected
    forbidden, bindings = router.routing_guard_contract(prompt)
    assert set(REGISTRY) - set(forbidden) == expected
    assert bindings == ({'web_search': {'query': prompt}} if 'web_search' in expected else {})


def actual_public_history_attempt(state, monkeypatch, mode, prompt, name, args, *, sid=None):
    """Public-web compatibility uses tool events even with planner enabled.

    This is an actual HTTP planner-menu/compatibility-loop observation, not
    evidence that the structured planner engine executed the public read.
    Other modes keep the ordinary schema-discovery/agent protocol.
    """
    if mode != 'planner':
        return actual_attempt(state, monkeypatch, mode, prompt, name, args, sid=sid)
    from service import main
    set_mode(monkeypatch, mode)
    script = [attempt(name, args), {'content': 'Synthetic public answer.'}]
    monkeypatch.setattr(main, 'OMLXClient', lambda **kwargs: state.Client(script))
    return state.run(prompt, script, sid=sid, approve=False, transport='asgi')


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt,name,expected', [
    ("what did Sarah's email say", 'view_emails', {'view_emails', 'summarize_emails'}),
    ('What did Alex’s messages say?', 'view_messages', {'view_messages', 'summarize_messages'}),
])
def test_actual_possessive_communication_speaker_keeps_existing_source_read(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, name, expected):
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    state = pending_entrypoint
    forbidden, bindings = routing_guard_contract(prompt)
    events = actual_attempt(state, monkeypatch, mode, prompt, name, {})
    (tmp_path / 'possessive-history-read-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'forbidden': sorted(forbidden), 'bindings': bindings,
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert set(REGISTRY) - set(forbidden) == expected and not bindings
    assert state.executed and all(n == name for n, _ in state.executed)
    assert not pending_rows(state) and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('speaker', ["Sarah's email", 'Sarah’s e-mail', "Alex's message",
                                    'Alex’s messages', "Alex's text", 'Alex’s texts', "Alex's iMessage"])
def test_possessive_communication_source_is_a_role_in_the_consumed_speaker(speaker):
    prompt = 'What did ' + speaker + ' say?'
    assert U.retrospective_communication_roles(prompt) == (True, speaker)
    assert not U.is_retrospective_communication_question('Text Mom saying "' + prompt + '"')
    assert not U.is_retrospective_communication_question(prompt + ' Then delete the Laundry reminder.')
    assert not U.is_retrospective_communication_question('Explain the phrase "' + prompt + '".')


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('prompt,cross_source,send,args', [
    ("what did Sarah's email say", 'view_messages', 'send_email',
     {'to': 'fictional@example.invalid', 'subject': 'fictional', 'body': 'Unrequested synthetic action'}),
    ('What did Alex’s messages say?', 'view_emails', 'send_message',
     {'to': '+15555550123', 'text': 'Unrequested synthetic action'}),
])
@pytest.mark.parametrize('kind', ['other-source', 'send', 'alias', 'opaque'])
def test_actual_possessive_communication_source_never_grants_other_sources_or_effects(
        pending_entrypoint, monkeypatch, tmp_path, mode, prompt, cross_source, send, args, kind):
    from service.tools.registry import REGISTRY
    state = pending_entrypoint
    monkeypatch.setitem(REGISTRY, 'fictional_unknown_read_alias', replace(
        REGISTRY['view_emails'], name='fictional_unknown_read_alias'))
    name, attempted = {'other-source': (cross_source, {}), 'send': (send, args),
        'alias': ('fictional_unknown_read_alias', {}),
        'opaque': ('run_shell', {'command': 'fictional'})}[kind]
    events = actual_attempt(state, monkeypatch, mode, prompt, name, attempted)
    (tmp_path / 'possessive-history-negative-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'attempt': {'name': name, 'args': attempted},
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests,
        'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert not state.executed and not state.source_calls and not state.native and not pending_rows(state)
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
def test_actual_possessive_history_words_remain_exact_current_message_body(
        pending_entrypoint, monkeypatch, tmp_path, mode):
    state = pending_entrypoint
    body = "What did Sarah's email say?"
    prompt = 'Text +15555550123 saying "' + body + '"'
    task = compile_task(prompt)
    assert task and task.subject.value == body and task.original_request == prompt
    events = actual_attempt(state, monkeypatch, mode, prompt, 'send_message',
                            {'to': '+15555550123', 'text': body})
    (tmp_path / 'possessive-history-literal-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'task': task.to_dict(), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    cards = [e for e in events if e['type'] == 'confirm' and e.get('tool') == 'send_message']
    assert len(cards) == 1 and cards[0]['args'] == {'to': '+15555550123', 'text': body}
    assert not state.executed and not state.source_calls and not state.native


@pytest.mark.parametrize('speaker', ["Trump's speech", 'Sarah’s report'])
def test_possessive_noun_alone_does_not_prove_a_private_communication_source(speaker):
    prompt = 'What did ' + speaker + ' say today?'
    assert U.retrospective_communication_roles(prompt) == (False, speaker)


GENERIC_ADVICE = [
    'How can I tell whether an email needs a reply?',
    '**How can I tell whether an email needs a reply?',
    '*How do I tell if a message requires a response?',
    '**How do I decide whether a text warrants a reply?**',
]


@pytest.mark.parametrize('head', ['How can I tell', 'How do I tell', 'How do I decide', 'How can I decide'])
@pytest.mark.parametrize('junction', ['whether', 'if'])
@pytest.mark.parametrize('noun', ['an email', 'an e-mail', 'a message', 'a text'])
@pytest.mark.parametrize('predicate,response', [('needs', 'reply'), ('requires', 'response'), ('warrants', 'reply')])
@pytest.mark.parametrize('marker', ['', '*', '**'])
def test_finite_generic_advice_frame_is_compositional_and_fully_consumed(head, junction, noun, predicate, response, marker):
    prompt = f'{marker}{head} {junction} {noun} {predicate} a {response}?'
    assert U.is_complete_explanation(prompt) and U.explanation_remainder(prompt) == ''
    assert not outbound_verb(prompt) and compile_new(prompt) is None and compile_task(prompt) is None


@pytest.mark.parametrize('prompt', [
    "How can I tell whether Sarah's email needs a reply?",
    'How can I tell whether this email needs a reply?',
    'How can I tell whether my email needs a reply?',
    'How do I decide if the message warrants a response?',
    'How can I tell whether an email from Alex needs a reply?',
    'How can I tell whether an email received today needs a reply?',
    'How can I tell whether an email needs a reply without reading Mail?',
    'How can I tell whether an email needs a reply? Then text Mom saying Thanks.',
    'Text Mom saying "How can I tell whether an email needs a reply?"',
    'say "How can I tell whether an email needs a reply?"',
    '"How can I tell whether an email needs a reply?"',
])
def test_generic_advice_abstains_on_actual_sources_literals_and_independent_actions(prompt):
    assert not U.is_complete_explanation(prompt)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('owner', ['reminder', 'reply-body', 'reply-target', 'workflow-addressed', 'workflow-unaddressed'])
@pytest.mark.parametrize('prompt', GENERIC_ADVICE)
def test_actual_generic_advice_preserves_all_pending_rows_before_source_or_preparation(
        pending_entrypoint, monkeypatch, tmp_path, mode, owner, prompt):
    from service.tools.registry import REGISTRY
    state = pending_entrypoint
    sid, _ = seed_pending(state, owner)
    for other in ['reminder', 'reply-body', 'reply-target', 'workflow-addressed', 'workflow-unaddressed']:
        seed_pending(state, other)
    before = pending_rows(state)
    monkeypatch.setitem(REGISTRY, 'fictional_advice_alias', replace(REGISTRY['view_emails'], name='fictional_advice_alias'))
    events = actual_public_history_attempt(state, monkeypatch, mode, prompt, 'fictional_advice_alias', {}, sid=sid)
    (tmp_path / 'advice-pending-receipt.json').write_text(json.dumps({
        'mode': mode, 'owner': owner, 'prompt': prompt, 'before': before, 'after': pending_rows(state),
        'protocol': 'empty-menu compatibility tool events with WISP_PLANNER=1; model-led discovery when selected',
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests, 'early': state.early,
        'source_calls': state.source_calls, 'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert pending_rows(state) == before
    assert not state.source_calls and not state.executed and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)
    assert any(m.get('role') == 'user' and m.get('content') == prompt
               for request in state.requests for m in request['messages'])


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('private_context', [False, True])
@pytest.mark.parametrize('name,args', [
    ('view_emails', {'unread': True}),
    ('send_message', {'to': '+15555550123', 'text': 'Unrequested synthetic action'}),
    ('clear_reminders', {'scope': 'all'}), ('run_shell', {'command': 'fictional'}),
    ('web_search', {'query': 'fictional private history'}),
])
def test_actual_complete_generic_advice_has_no_private_effect_or_egress_capabilities(
        pending_entrypoint, monkeypatch, tmp_path, mode, private_context, name, args):
    state = pending_entrypoint
    sid = state.sessions.create_session()
    if private_context:
        state.sessions.add_turn(sid, 'user', 'Fictional private history: secret-project-abc.')
        state.sessions.add_turn(sid, 'assistant', 'Fictional private history acknowledged.')
    prompt = '**How can I tell whether an email needs a reply?'
    events = actual_public_history_attempt(state, monkeypatch, mode, prompt, name, args, sid=sid)
    (tmp_path / 'advice-capability-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'private_context': private_context, 'attempt': {'name': name, 'args': args},
        'protocol': 'empty-menu compatibility tool events with WISP_PLANNER=1; model-led discovery when selected',
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests, 'plans': pending_rows(state),
        'source_calls': state.source_calls, 'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert not pending_rows(state) and not state.source_calls and not state.executed and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
@pytest.mark.parametrize('compound', [False, True])
def test_actual_generic_advice_literal_and_independent_send_preserve_original_body(
        pending_entrypoint, monkeypatch, tmp_path, mode, compound):
    state = pending_entrypoint
    body = '**How can I tell whether an email needs a reply?'
    authored = 'Text +15555550123 saying "' + body + '"'
    prompt = (GENERIC_ADVICE[0] + '; ' if compound else '') + authored
    task = compile_task(prompt)
    assert task and task.subject.value == body and task.original_request == prompt
    events = actual_attempt(state, monkeypatch, mode, prompt, 'send_message', {'to': '+15555550123', 'text': body})
    (tmp_path / 'advice-send-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'task': task.to_dict(), 'events': events,
        'raw_sse': state.raw_sse, 'requests': state.requests, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    cards = [e for e in events if e['type'] == 'confirm' and e.get('tool') == 'send_message']
    assert len(cards) == 1 and cards[0]['args'] == {'to': '+15555550123', 'text': body}
    assert not state.executed and not state.native


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
def test_actual_generic_advice_independent_read_preserves_supported_mail_owner(
        pending_entrypoint, monkeypatch, tmp_path, mode):
    state = pending_entrypoint
    prompt = GENERIC_ADVICE[0] + '; Show my unread email.'
    events = actual_attempt(state, monkeypatch, mode, prompt, 'view_emails', {'unread': True})
    (tmp_path / 'advice-read-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'events': events, 'raw_sse': state.raw_sse,
        'requests': state.requests, 'plans': pending_rows(state), 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert state.executed and all(name == 'view_emails' and args.get('unread') is True for name, args in state.executed)
    assert not pending_rows(state) and not state.source_calls and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model-led', 'shadow'])
def test_actual_generic_advice_unsupported_independent_action_clarifies_without_preparation(
        pending_entrypoint, monkeypatch, tmp_path, mode):
    state = pending_entrypoint
    state.reminders.add_manual('Laundry', time.time() + 3600)
    reminders = state.reminders.active_between()
    sid, _ = seed_pending(state, 'reply-target')
    before = pending_rows(state)
    prompt = GENERIC_ADVICE[0] + '; Delete the Laundry reminder from the Work account.'
    assert not U.is_complete_explanation(prompt)
    events = actual_public_history_attempt(state, monkeypatch, mode, prompt, 'clear_reminders', {'scope': 'all'}, sid=sid)
    (tmp_path / 'advice-unsupported-receipt.json').write_text(json.dumps({
        'mode': mode, 'prompt': prompt, 'before': before, 'after': pending_rows(state),
        'events': events, 'raw_sse': state.raw_sse, 'requests': state.requests, 'source_calls': state.source_calls,
        'registry_calls': state.executed, 'native_calls': state.native}, indent=2, default=str))
    assert pending_rows(state) == before and state.reminders.active_between() == reminders
    assert not state.source_calls and not state.executed and not state.native
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)
