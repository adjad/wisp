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
