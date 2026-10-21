"""Final answers over synthetic receipts through the production agent loop.

No model, native bridge, network or user cache is used. Scripted inference
deliberately drops qualifications so assertions check the published answer.
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
from dataclasses import replace

import pytest

from service.agent import loop
from service.tools.registry import REGISTRY
from tests.test_direct_dispatch_exec import Approver, ScriptedClient, _tool_call
from tests.test_workflow20_knowledge import knowledge_entrypoint, storage


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('WISP_HOME', str(tmp_path / 'wisp'))
    def forbidden(*args, **kwargs):
        raise AssertionError('No real model, native process or network in result projection tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(loop, 'audit', lambda *a, **kw: None)
    from service.safety import policy, grants
    monkeypatch.setattr(policy, '_FULL_ACCESS', True)
    monkeypatch.setattr(policy, '_READ_ONLY', False)
    monkeypatch.setattr(grants, 'check', lambda *a, **kw: None)


def run_receipts(monkeypatch, receipts, narration, *, prompt='What needs my attention in my inbox and messages?',
                 correction=None, approve=True, history=(), sequential=False, expected_calls=None, **options):
    """Actual tool-call protocol and run_tool dispatch; finite synthetic bodies."""
    calls = []
    script_calls = []
    outputs = {}
    for index, (name, args, result) in enumerate(receipts):
        outputs.setdefault(name, []).append(result)
        original = REGISTRY[name]
        def tool(*, _name=name, **kwargs):
            calls.append((_name, kwargs))
            return outputs[_name].pop(0)
        monkeypatch.setitem(REGISTRY, name, replace(original, func=tool))
        script_calls.append(_tool_call(name, args, f'fictional-{index}'))
    steps = ([{'tool_calls': [call]} for call in script_calls] if sequential else
             [{'tool_calls': script_calls}])
    if options.get('planner'):
        class PlannedClient(ScriptedClient):
            managed = True
            def __init__(self):
                super().__init__()
                self.plans = iter([{'thought': 'Read the requested synthetic result.', 'status': 'call_tools',
                    'calls': [{'tool': name, 'arguments': args} for name, args, _ in receipts]}])
                self.answers = iter([narration, correction if correction is not None else narration])
                self.plan_requests = 0
            async def stream_events(self, model, messages, **kwargs):
                self.calls.append(list(messages))
                if kwargs.get('response_format'):
                    self.plan_requests += 1
                    value = json.dumps(next(self.plans, {'thought': '', 'status': 'answer', 'calls': []}))
                else:
                    value = next(self.answers, narration)
                yield {'kind': 'content', 'text': value}
                yield {'kind': 'final', 'message': {'role': 'assistant', 'content': value, 'tool_calls': None}}
        client = PlannedClient()
    else:
        client = ScriptedClient([*steps, {'content': narration},
                                 {'content': correction if correction is not None else narration}])
    events = []
    async def emit(event):
        events.append(event)
    result = asyncio.run(loop.run_agent(client, 'Agents-A1-4B-oQe6',
        [*history, {'role': 'user', 'content': prompt}], emit, Approver(approve),
        tools=list(dict.fromkeys(name for name, _, _ in receipts)), max_steps=len(steps) + 2,
        include_memory_context=False, allow_skill_context=False, **options))
    if expected_calls is None:
        expected_calls = ([] if options.get('test_mode') or not approve else
                          [(name, args) for name, args, _ in receipts])
    assert calls == expected_calls
    # Published text is checked too: a final return value alone cannot retract
    # a previously shown false claim without a clear_answer event.
    published = ''
    for event in events:
        if event['type'] == 'clear_answer': published = ''
        elif event['type'] == 'text': published += event['text']
    assert published == result
    return result, client, events


def evidence(*, omitted=49, messages='unavailable', records=None):
    return json.dumps({'task_window': {'period': 'today'}, 'sources': {
        'email': {'state': 'ready', 'records': records or [{
            'subject': 'Fictional review request', 'body': 'Please review the blue form.',
            'sender_name': 'Maya', 'received_at': 1792494000}], 'omitted': omitted},
        'messages': {'state': messages, 'records': [], 'reason': 'Synthetic source unavailable'}},
        'excluded_sources': ['calendar', 'reminders', 'notes']})


def test_partial_inbox_cannot_publish_no_other_items_need_attention(monkeypatch):
    text, client, _ = run_receipts(monkeypatch, [('collect_task_evidence', {}, evidence())],
        'Maya asks you to review the blue form. No other items need attention.')
    assert 'No other items need attention' not in text
    assert '49 omitted' in text and 'Messages: unavailable' in text
    assert len(client.calls) == 3  # one bounded correction; no repeated read


@pytest.mark.parametrize('mode', ['normal', 'planner'])
@pytest.mark.parametrize('corrected', [False, True])
def test_live_negative_task_claim_uses_one_correction_without_new_reads(monkeypatch, mode, corrected):
    claim = 'No active requests or deadlines in the email/messages sources.'
    useful = ('The returned excerpt asks you to review the blue form. '
              'I cannot establish whether other requests or deadlines exist.')
    text, client, _ = run_receipts(monkeypatch,
        [('collect_task_evidence', {}, evidence())], claim,
        correction=useful if corrected else claim, planner=mode == 'planner')
    assert claim not in text and '49 omitted' in text and 'Messages: unavailable' in text
    if corrected:
        assert text.startswith(useful)
    else:
        assert 'Fictional review request' not in text  # no private bodies in deterministic fallback
    if mode == 'planner':
        assert client.plan_requests == 2  # one read plan, then an answer plan
        assert len(client.calls) == 4  # both plans, narration, one correction
    else:
        assert len(client.calls) == 3  # read, narration, one tool-free correction


@pytest.mark.parametrize('mode', ['normal', 'planner', 'model_led'])
@pytest.mark.parametrize('receipt', [
    'Reminder set: “Review blue form” — tomorrow 09:15 in Wisp only; Apple Reminders was not changed (Reminders access is unavailable).',
    'Reminder set: “Review blue form” — tomorrow 09:15 in Apple Reminders and Wisp.',
])
def test_generic_create_publishes_exact_local_or_native_receipt(monkeypatch, receipt, mode):
    args = {'title': 'Review blue form', 'when_iso': '2099-10-21T09:15'}
    claims, settlements = [], []
    def claim(tool, values):
        claims.append((tool.name, values))
        return {'admitted': True, 'id': 'fictional-owned-effect'}
    def finish(claim, verified):
        settlements.append((claim, verified))
        return True
    flags = {'planner': True} if mode == 'planner' else ({
        'model_led_discovery': True, 'model_led_preload': ('add_reminder',),
        'claim_effect': claim, 'finish_effect': finish} if mode == 'model_led' else {})
    text, client, _ = run_receipts(monkeypatch, [('add_reminder', args, receipt)],
        "I've created your reminder in Apple Reminders.", prompt='Remind me to review blue form tomorrow at 09:15.', **flags)
    assert text == receipt
    if mode == 'planner':
        assert client.plan_requests >= 1
    elif mode == 'model_led':
        assert claims == [('add_reminder', args)] and settlements == [({'admitted': True, 'id': 'fictional-owned-effect'}, True)]


@pytest.mark.parametrize('claim', [
    'No other items need attention.', 'No other messages require your attention.',
    'No other emails need my attention.',
])
@pytest.mark.parametrize('omitted,messages', [(49, 'unavailable'), (0, 'ready')])
def test_bounded_coverage_never_proves_global_absence(monkeypatch, claim, omitted, messages):
    text, _, _ = run_receipts(monkeypatch, [('collect_task_evidence', {},
        evidence(omitted=omitted, messages=messages))], claim)
    assert claim not in text and 'absence not verified' in text


@pytest.mark.parametrize('omitted,messages', [(49, 'unavailable'), (0, 'ready')])
def test_corrected_actionable_answer_preserves_personal_and_promo_distinction(monkeypatch, omitted, messages):
    useful = ('Maya asks you to review the blue form. The sale is promotional; '
              'among the returned excerpts, prioritize Maya. I cannot establish what else needs attention.')
    data = evidence(omitted=omitted, messages=messages, records=[
        {'subject': 'Fictional personal request', 'body': 'Maya asks you to review the blue form.'},
        {'subject': 'Fictional promotion', 'body': 'Shop our sale.'}])
    text, client, _ = run_receipts(monkeypatch, [('collect_task_evidence', {}, data)],
        'Maya asks you to review the blue form. No other items need attention.', correction=useful)
    assert text.startswith(useful) and 'No other items need attention.' not in text
    assert 'Source coverage' in text and len(client.calls) == 3


def test_legitimate_bounded_selected_item_answer_is_unchanged(monkeypatch):
    useful = ('The only item shown is Maya’s review request. '
              'No other shown items need attention; omitted mail and unavailable Messages are unverified.')
    text, client, _ = run_receipts(monkeypatch, [('collect_task_evidence', {}, evidence())], useful)
    assert text.startswith(useful) and len(client.calls) == 2


def test_quoted_source_absence_is_not_an_assistant_absence_claim(monkeypatch):
    useful = ('Maya wrote "No other items need attention." That is the source statement, '
              'not verified current inbox coverage.')
    text, client, _ = run_receipts(monkeypatch, [('collect_task_evidence', {}, evidence())], useful)
    assert text.startswith(useful) and len(client.calls) == 2


def test_generic_create_retains_required_evidence_coverage_without_raw_bodies(monkeypatch):
    receipt = 'Reminder set: “Blue form” — tomorrow in Wisp only; Apple Reminders was not changed (denied).'
    data = evidence(records=[{'subject': 'Fictional review', 'body': 'PRIVATE_SYNTHETIC_BODY_26e8'}])
    text, _, _ = run_receipts(monkeypatch, [('collect_task_evidence', {}, data),
        ('add_reminder', {'title': 'Blue form', 'when_iso': '2099-10-21T09:15'}, receipt)],
        'No other items need attention and the reminder is in Apple Reminders.',
        required_tool_groups=(frozenset({'collect_task_evidence'}), frozenset({'add_reminder'})), sequential=True)
    assert receipt in text and '49 omitted' in text and 'Messages: unavailable' in text
    assert 'PRIVATE_SYNTHETIC_BODY_26e8' not in text and '"task_window"' not in text


@pytest.mark.parametrize('receipt', [
    '(error: native outcome unknown; nothing was confirmed. Check Reminders before retrying.)',
    '(error: Reminder creation failed; nothing was added.)',
])
def test_failure_and_unknown_are_not_narrated_as_success(monkeypatch, receipt):
    text, _, _ = run_receipts(monkeypatch,
        [('add_reminder', {'title': 'Blue form', 'when_iso': '2099-10-21T09:15'}, receipt)],
        'Done, the reminder is in Apple Reminders.', prompt='Remind me about the blue form.')
    assert 'Done, the reminder' not in text and receipt.removeprefix('(error: ').rstrip(')') in text


@pytest.mark.parametrize('dry_run,approve', [(True, True), (False, False)])
def test_dry_run_and_denial_publish_no_success_receipt(monkeypatch, dry_run, approve):
    from service.safety import policy
    monkeypatch.setattr(policy, '_FULL_ACCESS', False)
    if not approve:
        monkeypatch.setattr(policy, '_READ_ONLY', True)
    receipt = 'Reminder set: “Blue form” — tomorrow in Apple Reminders and Wisp.'
    text, _, _ = run_receipts(monkeypatch,
        [('add_reminder', {'title': 'Blue form', 'when_iso': '2099-10-21T09:15'}, receipt)],
        'Done, the reminder is in Apple Reminders.', prompt='Remind me about the blue form.',
        test_mode=dry_run, approve=approve)
    assert receipt not in text and 'Done, the reminder' not in text
    assert ('Dry run only' in text) if dry_run else ('not completed' in text)


def test_mixed_required_read_and_local_create_keep_both_receipts(monkeypatch):
    read = 'Tomorrow: Fictional review at 10:00. Calendar coverage is bounded.'
    receipt = 'Reminder set: “Blue form” — tomorrow in Wisp only; Apple Reminders was not changed (denied).'
    text, _, _ = run_receipts(monkeypatch, [('get_upcoming', {'period': 'tomorrow'}, read),
        ('add_reminder', {'title': 'Blue form', 'when_iso': '2099-10-21T09:15'}, receipt)],
        'Your day is clear and the reminder is in Apple Reminders.',
        prompt='Check tomorrow and remind me about the blue form.',
        required_tool_groups=(frozenset({'get_upcoming'}), frozenset({'add_reminder'})), sequential=True)
    assert read in text and receipt in text
    assert 'Your day is clear' not in text


def test_missing_required_read_does_not_claim_the_whole_task_finished(monkeypatch):
    receipt = 'Reminder set: “Blue form” — tomorrow in Wisp only; Apple Reminders was not changed (denied).'
    text, _, _ = run_receipts(monkeypatch,
        [('add_reminder', {'title': 'Blue form', 'when_iso': '2099-10-21T09:15'}, receipt)],
        'Everything requested is complete.', prompt='Check tomorrow and remind me about blue form.',
        required_tool_groups=(frozenset({'add_reminder'}), frozenset({'get_upcoming'})))
    assert receipt in text and "couldn't complete every requested step" in text
    assert 'Everything requested is complete' not in text


def test_existing_duplicate_effect_guard_still_keeps_the_first_receipt(monkeypatch):
    first = 'Reminder set: “Blue form” — tomorrow in Wisp only; Apple Reminders was not changed (denied).'
    second = '(error: Green form native outcome unknown; nothing was confirmed.)'
    text, _, _ = run_receipts(monkeypatch, [
        ('add_reminder', {'title': 'Blue form', 'when_iso': '2099-10-21T09:15'}, first),
        ('add_reminder', {'title': 'Green form', 'when_iso': '2099-10-21T09:15'}, second)],
        'Both reminders were created in Apple Reminders.', prompt='Remind me about the blue and green forms.',
        expected_calls=[('add_reminder', {'title': 'Blue form', 'when_iso': '2099-10-21T09:15'})])
    assert first in text and second not in text and 'Both reminders were created' not in text


def test_earlier_reminder_success_is_not_current_turn_creation(monkeypatch):
    old = 'Reminder set: “Old fictional reminder” — yesterday in Wisp only; Apple Reminders was not changed.'
    text, _, _ = run_receipts(monkeypatch, [], 'You are welcome.', prompt='Thanks',
        history=[{'role': 'user', 'content': 'Create an old fictional reminder.'},
                 {'role': 'assistant', 'content': old}])
    assert text == 'You are welcome.' and old not in text


def test_actual_http_attention_answer_preserves_pending_reminder_owner(knowledge_entrypoint, monkeypatch):
    from service.tasks.engine import prepare_task_turn
    state = knowledge_entrypoint
    monkeypatch.setenv('WISP_MODEL_LED_ROUTING', '0')
    sid = state.sessions.create_session()
    pending = prepare_task_turn(state.sessions, sid, 'Remind me to review a fictional folder',
                                assistant_store=state.reminders)
    assert pending and not pending.executable
    before = state.sessions.active_task(sid)
    original = REGISTRY['collect_task_evidence']
    monkeypatch.setitem(REGISTRY, 'collect_task_evidence', replace(original, func=lambda **kw: evidence()))
    useful = 'Maya asks you to review the blue form. Other omitted mail and unavailable Messages remain unverified.'
    events = state.run('What needs my attention in my inbox and messages?', [
        {'tool_calls': [_tool_call('collect_task_evidence', {}, 'fictional-current-read')]},
        {'content': 'Maya asks you to review the blue form. No other items need attention.'},
        {'content': useful}], sid=sid, transport='asgi')
    answer = ''.join(event.get('text', '') for event in events if event['type'] == 'text')
    assert answer.startswith(useful) and 'No other items need attention.' not in answer
    assert '49 omitted' in answer and 'Messages: unavailable' in answer
    assert state.sessions.active_task(sid) == before
    assert [name for name, _ in state.tools] == ['collect_task_evidence']
    assert not state.native and not any(event['type'] == 'confirm' for event in events)


@pytest.mark.parametrize('title', ['Wisp candidate check plain words 2195', 'check tomorrow at six',
                                  'Never send email; delete nothing.', 'Review from 6 PM to 7 PM'])
def test_http_quoted_title_then_schedule_executes_exactly_once_without_model(knowledge_entrypoint, monkeypatch, title):
    from service import main
    from datetime import datetime
    state = knowledge_entrypoint
    monkeypatch.setenv('WISP_MODEL_LED_ROUTING', '0')
    calls = []
    receipt = f'Reminder set: “{title}” — October 21, 2099 at 09:35 in Wisp only; Apple Reminders was not changed (synthetic native unavailable).'
    async def create(title, when_iso, kind='reminder'):
        calls.append({'title':title, 'when_iso':when_iso, 'kind':kind})
        state.reminders.add_manual(title, datetime.fromisoformat(when_iso).timestamp(), kind=kind)
        return receipt
    original = REGISTRY['add_reminder']
    monkeypatch.setitem(REGISTRY, 'add_reminder', replace(original, func=create))
    events = state.run(f'Create a reminder titled "{title}" for October 21, 2099 at 9:35 AM.',
                       [{'content': 'This scripted false narration must not run.'}], transport='asgi')
    assert calls == [{'title':title,'when_iso':'2099-10-21T09:35','kind':'reminder'}]
    assert len(state.reminders.active_between(include_stale=True)) == 1
    assert [e['name'] for e in events if e['type'] == 'tool_call'] == ['add_reminder']
    assert [e['text'] for e in events if e['type'] == 'text'] == [receipt]
    assert len([e for e in events if e['type'] == 'done']) == 1
    assert not state.clients[0].calls and not state.native
    main.ensure_omlx.assert_not_awaited()


@pytest.mark.parametrize('suffix', [
    'for October 21, 2099 at 9:35 AM and send email Mom saying hello.',
    'for October 21, 2099 at 9:35 AM; delete another reminder.',
    'for October 21, 2099 at 9:35 AM if Sam agrees.',
    'for October 21, 2099 at 9:35 AM or 10 AM.',
    'for October 21, 2099 at 25:99 AM.', 'for February 30, 2099 at 9:35 AM.',
    'for October 21, 2099.', 'at 9:35 AM.', 'tomorrow at', 'tomorrow at 9',
    'for October 21, 2099 at 9:35.',
])
def test_http_unresolved_quoted_schedule_is_host_clarification_without_inference(knowledge_entrypoint, monkeypatch, suffix):
    from service import main
    state = knowledge_entrypoint
    monkeypatch.setenv('WISP_MODEL_LED_ROUTING', '0')
    events = state.run('Create a reminder titled "Fictional blue form" ' + suffix,
        [{'content':'Reminder set for tomorrow. What should it remind you to do?'}], transport='asgi')
    answer = ''.join(e.get('text','') for e in events if e['type'] in {'text','delta'})
    assert answer.startswith('I haven’t created a reminder or taken any other action.')
    assert 'Reminder set' not in answer
    assert not state.tools and not state.native and not state.clients[0].calls
    assert state.reminders.active_between(include_stale=True) == []
    assert not any(e['type'] in {'tool_call','confirm','delta'} for e in events)
    assert len([e for e in events if e['type'] == 'done']) == 1
    main.ensure_omlx.assert_not_awaited()
    main.maybe_summarize.assert_not_awaited()


@pytest.mark.parametrize('mode', ['legacy', 'planner', 'shadow'])
def test_host_clarification_keeps_pending_owner_and_persisted_reply(knowledge_entrypoint, monkeypatch, mode):
    from service.tasks.engine import prepare_task_turn
    from tests.test_direct_dispatch_exec import ScriptedClient
    state = knowledge_entrypoint
    monkeypatch.setenv('WISP_MODEL_LED_ROUTING', '0')
    if mode == 'planner':
        monkeypatch.setenv('WISP_PLANNER', '1')
        monkeypatch.setattr(ScriptedClient, 'managed', True, raising=False)
    if mode == 'shadow':
        monkeypatch.setenv('WISP_TYPED_REMINDERS_SHADOW_ONLY', '1')
    sid = state.sessions.create_session()
    pending = prepare_task_turn(state.sessions, sid, 'Remind me to review a fictional folder',
                                assistant_store=state.reminders)
    assert pending and not pending.executable
    before = state.sessions.active_task(sid)
    events = state.run('Create a reminder titled "Fictional blue form" for October 21, 2099.',
        [{'content':'Reminder set.'}], sid=sid, transport='asgi')
    text = ''.join(e.get('text','') for e in events if e['type'] in {'text','delta'})
    assert text.startswith('I haven’t created a reminder or taken any other action.')
    assert state.sessions.active_task(sid) == before
    assert not state.tools and not state.native and not state.clients[0].calls
    history = state.sessions.turns_from(sid, 0)
    assert history[-1]['role'] == 'assistant' and history[-1]['content'] == text


@pytest.mark.parametrize('prompt', [
    'Create a reminder titled "Fictional blue form".',
    'Create a reminder titled "" for October 21, 2099 at 9:35 AM.',
])
def test_http_incomplete_quoted_reminder_uses_existing_slot_clarification(knowledge_entrypoint, monkeypatch, prompt):
    state = knowledge_entrypoint
    monkeypatch.setenv('WISP_MODEL_LED_ROUTING', '0')
    events = state.run(prompt, [{'content':'Reminder set.'}], transport='asgi')
    text = ''.join(e.get('text','') for e in events if e['type'] in {'text','delta'})
    assert text and '?' in text and 'Reminder set' not in text
    assert not state.tools and not state.native and not state.clients[0].calls
    assert state.reminders.active_between(include_stale=True) == []
