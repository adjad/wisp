"""Legacy topical Mail ownership, through parser and actual inert /agent.

All registered execution functions are inert while retaining module metadata
for real source-family classification. The shared entrypoint fixture blocks
sockets/processes, uses disposable stores, and denies every confirmation.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
from dataclasses import replace

import pytest

from service.router import router as R
from service.tools.registry import REGISTRY
from tests.test_direct_dispatch_exec import _tool_call
from tests.test_workflow20_knowledge import storage, knowledge_entrypoint  # noqa: F401

PREFIX = 'What is in my email about '
COMPLETE = [
    ('rent today?', {'query': 'rent', 'day': 'today'}),
    ('rent from yesterday?', {'query': 'rent', 'day': 'yesterday'}),
    ('rent during this week?', {'query': 'rent', 'period': 'this week'}),
    ('rent on last month?', {'query': 'rent', 'period': 'last month'}),
    ('today?', {'query': 'today'}),
    ('anything today?', {'query': 'today'}),
    ('"today"?', {'query': 'today'}),
    ('"line one\nline two"?', {'query': 'line one\nline two'}),
    ('anything fiancial?', {'query': 'financial'}),
    ('anything securty?', {'query': 'security'}),
    ('security-team?', {'query': 'security-team'}),
    ('"today" last week?', {'query': 'today', 'period': 'last week'}),
    ('"letters from home" from yesterday?', {'query': 'letters from home', 'day': 'yesterday'}),
    ('"rent; do not browse"?', {'query': 'rent; do not browse'}),
    ('“securty, today!”?', {'query': 'securty, today!'}),
    ('"Mom’s rent"?', {'query': 'Mom’s rent'}),
    ("'Mom’s rent' yesterday?", {'query': 'Mom’s rent', 'day': 'yesterday'}),
    ('‘Mom’s rent’ yesterday?', {'query': 'Mom’s rent', 'day': 'yesterday'}),
    ('`rent, today!` last month?', {'query': 'rent, today!', 'period': 'last month'}),
    ('"notes; do not read my email" today?',
     {'query': 'notes; do not read my email', 'day': 'today'}),
    ('"receipts" today?', {'query': 'receipts', 'day': 'today'}),
    ('"find files"?', {'query': 'find files'}),
    ('"remind me tomorrow"?', {'query': 'remind me tomorrow'}),
    ('"send it"?', {'query': 'send it'}),
    ('"draft an email"?', {'query': 'draft an email'}),
    ('"cancel scheduled messages"?', {'query': 'cancel scheduled messages'}),
    ('"text Mom the answer"?', {'query': 'text Mom the answer'}),
]
COMPLETE += [
    (f'"rent" {connector}{receipt}?', {'query': 'rent',
     'day' if receipt in {'today', 'yesterday'} else 'period': receipt})
    for connector in ('', 'for ', 'from ', 'during ', 'on ')
    for receipt in ('today', 'yesterday', 'this week', 'last week', 'this month', 'last month')
]

INCOMPLETE = [
    'rent excluding receipts?', 'rent from my work account?',
    'rent and summarize it in one sentence?', 'food and drink?',
    'rent today yesterday?', '"rent" today yesterday?', 'rent; only use Gmail?',
    'rent this morning?', 'rent at 10am?', '"rent"; find files?',
    'rent and text Mom?', '"rent" extra?', '"rent?', '""?',
    'rent and food?', 'rent; and email?', '?', '', '"rent"today?', '"rent". today?',
    'rent; What is in my email about food?',
]


def exact(args):
    return {'count': 10, 'strict_match': True, **args}


@pytest.mark.parametrize('tail,args', COMPLETE)
def test_complete_parser_preserves_topic_and_receipt(tail, args):
    prompt = PREFIX + tail
    contract = R._topical_mail_contract(prompt)
    assert contract.state == 'complete'
    assert contract.original == prompt
    assert contract.arguments() == exact(args)
    if contract.literal_span:
        assert prompt[slice(*contract.literal_span)][1:-1] == args['query']


@pytest.mark.parametrize('tail', INCOMPLETE)
def test_incomplete_parser_never_claims_partial_arguments(tail):
    contract = R._topical_mail_contract(PREFIX + tail)
    assert contract.state == 'incomplete'
    assert R._topical_email_args(PREFIX + tail) is None


def test_non_mail_contract_is_unrecognized():
    assert R._topical_mail_contract('What is on my calendar?').state == 'unrecognized'


@pytest.fixture
def mail_entrypoint(monkeypatch, request):
    from tests import test_direct_dispatch_exec as client_module
    from service import main

    class CapturingClient(client_module.ScriptedClient):
        async def stream_events(self, model, messages, **kw):
            self.schemas = getattr(self, 'schemas', []) + [copy.deepcopy(kw.get('tools'))]
            self.choices = getattr(self, 'choices', []) + [kw.get('tool_choice')]
            async for event in super().stream_events(model, messages, **kw):
                yield event

    monkeypatch.setattr(client_module, 'ScriptedClient', CapturingClient)
    monkeypatch.setenv('WISP_MODEL_LED_ROUTING', '0')
    monkeypatch.setenv('WISP_PLANNER', '0')
    state = request.getfixturevalue('knowledge_entrypoint')
    state.executed = []
    state.order = []
    state.decisions = []
    state.result = 'Email synthetic-mail-1\nFrom: synthetic@example.test\nBody: Synthetic matched record.'
    for name, tool in list(REGISTRY.items()):
        def inert(_name=name, **args):
            state.executed.append((_name, copy.deepcopy(args)))
            state.order.append(('execute', _name))
            return state.result if _name == 'view_emails' else 'SYNTHETIC INERT ' + _name
        inert.__module__ = tool.func.__module__
        monkeypatch.setitem(REGISTRY, name, replace(tool, func=inert))
    original_route = main.route
    async def route(*args, **kw):
        decision = await original_route(*args, **kw)
        state.decisions.append(copy.deepcopy(decision))
        return decision
    monkeypatch.setattr(main, 'route', route)
    original_stream = CapturingClient.stream_events
    async def stream(client, model, messages, **kw):
        state.order.append(('inference', copy.deepcopy(messages)))
        async for event in original_stream(client, model, messages, **kw):
            yield event
    monkeypatch.setattr(CapturingClient, 'stream_events', stream)
    yield state
    if trace_dir := os.environ.get('WISP_TOPICAL_TRACE_DIR'):
        trace = Path(trace_dir)
        trace.mkdir(parents=True, exist_ok=True)
        (trace / (request.node.name.replace('/', '_') + '.json')).write_text(json.dumps({
            'events': state.events, 'decisions': [d.as_dict() for d in state.decisions],
            'executed': state.executed, 'loop_tools': state.tools, 'native': state.native,
            'early': state.early, 'order': state.order,
            'inference': [{'messages': c.calls, 'schemas': getattr(c, 'schemas', []),
                           'choices': getattr(c, 'choices', [])} for c in state.clients],
        }, ensure_ascii=False, indent=2))


def assert_clean_execution(d, prompt, complete=False, permitted=True):
    assert d.resolved_request == prompt
    assert d.tool_subset == (['view_emails'] if permitted else [])
    assert set(REGISTRY) - {'view_emails'} <= d.forbidden_tools
    assert d.force_first_tool is None and not d.expect_tool_first
    assert d.conditional_tools == ()
    assert not d.reminder_action and not d.isolated_public_query
    assert not d.clarify_channel and not d.clarify_target
    assert d.verified_results_only
    assert d._topical_mail_owner is not None
    assert '_topical_mail_owner' not in d.as_dict()
    if complete:
        assert d.required_tool_groups == (frozenset({'view_emails'}),)
        assert d.narration_after == frozenset({'view_emails'})
        assert d.strict_read_limits == {'view_emails': 1}
    else:
        assert not d.direct_calls and not d.required_tool_groups
        assert not d.tool_argument_bindings and not d.narration_after
        assert not d.strict_read_limits


def assert_safe_events(state, events):
    assert state.native == []
    assert not any(e['type'] in {'confirm', 'error', 'task_plan'} for e in events), events
    assert events[-1]['type'] == 'done'
    assert state.tools == state.executed


@pytest.mark.parametrize('tail,args', COMPLETE)
def test_actual_complete_positive_is_one_bound_read_then_narration(mail_entrypoint, tail, args):
    state = mail_entrypoint
    prompt = PREFIX + tail
    # The returned content contains the literal topic but no receipt-date word
    # unless that word is itself the requested topic.
    state.result = 'Email synthetic-mail-1\nBody: Verified ' + args['query'] + ' record.'
    events = state.run(prompt, [{'content': 'Verified Mail result.'}], transport='asgi')
    assert state.executed == [('view_emails', exact(args))]
    assert state.order[0] == ('execute', 'view_emails')
    assert len(state.clients[-1].calls) == 1
    first = state.clients[-1].calls[0]
    assert any(m.get('role') == 'tool' and m.get('content') == state.result for m in first)
    assert [m['content'] for m in first if m['role'] == 'user'] == [prompt]
    assert_clean_execution(state.decisions[-1], prompt, complete=True)
    assert state.decisions[-1].tool_argument_bindings == {'view_emails': exact(args)}
    assert_safe_events(state, events)


@pytest.mark.parametrize('tail,args', COMPLETE[:5])
def test_actual_strict_no_match_does_not_retry_or_infer(mail_entrypoint, tail, args):
    state = mail_entrypoint
    state.result = 'No emails matching “synthetic” were found in the recent available body snapshot.'
    events = state.run(PREFIX + tail, [{'content': 'Must not run'}], transport='asgi')
    assert state.executed == [('view_emails', exact(args))]
    assert state.clients[-1].calls == []
    assert_safe_events(state, events)


@pytest.mark.parametrize('tail', [x for x in INCOMPLETE if x not in {
    'rent and text Mom?', 'rent; and email?'}])
def test_actual_incomplete_preserves_request_and_accepts_one_auto_clarification(mail_entrypoint, tail):
    state = mail_entrypoint
    prompt = PREFIX + tail
    events = state.run(prompt, [{'content': 'Please clarify the Mail scope.'}], transport='asgi')
    assert state.executed == []
    client = state.clients[-1]
    assert len(client.calls) == 1
    assert client.choices == ['auto']
    assert [[x['function']['name'] for x in ss] for ss in client.schemas] == [['view_emails']]
    assert [m['content'] for m in client.calls[0] if m['role'] == 'user'] == [prompt]
    assert_clean_execution(state.decisions[-1], prompt)
    assert_safe_events(state, events)


def test_actual_interpreted_topic_can_call_mail_after_reading_original(mail_entrypoint):
    state = mail_entrypoint
    prompt = PREFIX + 'food and drink?'
    args = {'query': 'food and drink', 'count': 10, 'strict_match': True}
    events = state.run(prompt, [{'tool_calls': [_tool_call('view_emails', args, 'interpreted')]},
                               {'content': 'Verified Mail result.'}], transport='asgi')
    assert state.order[0][0] == 'inference'
    assert state.executed == [('view_emails', args)]
    assert len(state.clients[-1].calls) == 2
    assert_clean_execution(state.decisions[-1], prompt)
    assert_safe_events(state, events)


def test_actual_mail_denial_rejects_invented_reader_before_sink(mail_entrypoint):
    state = mail_entrypoint
    prompt = PREFIX + 'rent; do not read my email?'
    events = state.run(prompt, [{'tool_calls': [_tool_call('view_emails', {'query': 'rent'}, 'denied')]},
                               {'content': 'Email was excluded. Please clarify.'}], transport='asgi')
    assert state.executed == []
    assert_clean_execution(state.decisions[-1], prompt, permitted=False)
    assert_safe_events(state, events)


FORBIDDEN = [
    ('find_files', {'query': 'rent'}), ('read_file', {'path': '/synthetic/only'}),
    ('draft_email', {'to': 'synthetic@example.test', 'subject': 'Synthetic', 'body': 'Synthetic'}),
    ('send_message', {'recipient': 'synthetic@example.test', 'text': 'Synthetic'}),
    ('search_notes', {'query': 'rent'}), ('view_messages', {'query': 'rent'}),
    ('web_search', {'query': 'rent'}), ('run_shell', {'cmd': 'pwd'}),
]


@pytest.mark.parametrize('name,args', FORBIDDEN)
def test_actual_hallucinated_tool_rejected_before_every_sink(mail_entrypoint, name, args):
    state = mail_entrypoint
    prompt = PREFIX + 'rent excluding receipts?'
    events = state.run(prompt, [{'tool_calls': [_tool_call(name, args, 'forbidden')]},
                               {'content': 'Please clarify the Mail scope.'}], transport='asgi')
    assert state.executed == []
    client = state.clients[-1]
    assert len(client.calls) == 2
    assert all(choice == 'auto' for choice in client.choices)
    assert all([t['function']['name'] for t in ss] == ['view_emails'] for ss in client.schemas)
    assert any(e['type'] == 'tool_result' and name in str(e) for e in events)
    assert_clean_execution(state.decisions[-1], prompt)
    assert_safe_events(state, events)


@pytest.mark.parametrize('prompt,complete', [
    (PREFIX + 'rent; do not browse?', True),
    ('Don’t use the web; ' + PREFIX + 'rent excluding receipts?', False),
    (PREFIX + 'rent excluding receipts; do not read my notes or messages?', False),
    ('Do not read my notes or messages and ' + PREFIX + 'rent?', True),
    (PREFIX + 'rent without reading my notes?', True),
])
def test_actual_outside_policy_keeps_only_mail(mail_entrypoint, prompt, complete):
    state = mail_entrypoint
    events = state.run(prompt, [{'content': 'Please clarify the Mail scope.'}], transport='asgi')
    assert len(state.executed) == int(complete)
    assert_clean_execution(state.decisions[-1], prompt, complete=complete)
    assert_safe_events(state, events)


def test_actual_mail_denial_has_no_read_or_effect_capability(mail_entrypoint):
    state = mail_entrypoint
    prompt = PREFIX + 'rent; do not read my email?'
    events = state.run(prompt, [{'content': 'Email was excluded. Which source should I use?'}], transport='asgi')
    assert state.executed == []
    assert 'view_emails' in state.decisions[-1].forbidden_tools
    assert_clean_execution(state.decisions[-1], prompt, permitted=False)
    assert 'Email was excluded' in str(events)
    assert_safe_events(state, events)


@pytest.mark.parametrize('tail', ['rent and text Mom the answer?', 'rent and text Mom?', 'rent; and email?'])
def test_actual_earlier_effect_workflow_keeps_ownership(mail_entrypoint, tail):
    state = mail_entrypoint
    events = state.run(PREFIX + tail,
                       [{'content': 'Must not run'}], transport='asgi')
    assert any(e['type'] == 'workflow' for e in events)
    assert state.executed == [] and state.decisions == []
    assert state.clients[-1].calls == []
    assert_safe_events(state, events)


def test_unavailable_mail_clears_complete_obligations(monkeypatch):
    monkeypatch.setitem(REGISTRY, 'view_emails', replace(
        REGISTRY['view_emails'], unavailable_reason='Synthetic unavailable'))
    prompt = PREFIX + 'rent today?'
    d = asyncio.run(R.route(prompt))
    assert_clean_execution(d, prompt, permitted=False)
