"""Synthetic regressions for scoped outbound content; never use native apps."""
import asyncio
from types import SimpleNamespace

import pytest

from service.memory.store import SessionStore
from service.safety.policy import Decision, Tier
from service.tools.registry import REGISTRY, Tool
from service.workflows import executor
from service.workflows.compiler import compile_new, compile_decision
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
            'properties': {'day': {'type': 'string'}, 'period': {'type': 'string'}}},
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
    return asyncio.run(executor.execute_workflow(plan, state.emit, state.approver))


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
def test_plain_pronoun_preserves_exact_answer(pronoun):
    plan = compile_new(f'send {pronoun} to Mom via Messages',
                       last_user='daily summary', last_assistant=STALE)
    assert plan.artifact_text == STALE and not plan.sources
    assert compile_decision(plan).tool_argument_bindings['send_message']['text'] == STALE


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
    first = prepare_turn(store, sid, 'send it to Mom')
    assert first.plan.artifact_text == STALE
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
