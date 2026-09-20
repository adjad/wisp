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
    with tempfile.TemporaryDirectory(prefix='wisp-effect-claim-') as root:
        store = SessionStore(Path(root) / 'sessions.db')
        try:
            return asyncio.run(executor.execute_workflow(plan, state.emit, state.approver, store=store))
        finally:
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
    monkeypatch.setattr(store, 'claim_effect_call', fail)
    delivery.allow = True
    plan = compile_new('send my email summary to Mom via Messages')
    plan.status = 'running'
    result = asyncio.run(executor.execute_workflow(plan, delivery.emit, delivery.approver, store=store))
    assert result.status == 'failed' and not delivery.effects
    store._db.close()


def test_denial_does_not_consume_effect_claim(tmp_path, delivery):
    store = SessionStore(tmp_path / 'claims.db')
    plan = compile_new('send my email summary to Mom via Messages')
    plan.status = 'running'
    result = asyncio.run(executor.execute_workflow(plan, delivery.emit, delivery.approver, store=store))
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
        asyncio.run(executor.execute_workflow(plan, emit, SimpleNamespace(confirm=approve), store=store))
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
                                      SimpleNamespace(confirm=simultaneous_approval), store=store)
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
    'send my messages with Alice to Mom via Messages',
    'send my messages yesterday from Alice to Mom via Messages',
    'send my messages from yesterday from Alice to Mom via Messages',
    'send my messages from last week with Alice to Mom via Messages',
    'send my text yesterday from Alice to Mom via Messages',
    'send my message last week from Alice to Mom via Messages',
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


@pytest.mark.parametrize('prompt,is_daily', [
    ('only text my daily summary to Mom via Messages', True),
    ('just text my daily summary to Mom via Messages', True),
    ('send only the messages from Alice to Mom via Messages', False),
    ('send a summary of messages from Alice to Bob via email', False),
    ('send my messages yesterday from Alice to Mom via Messages', False),
    ("send Alice's messages to Mom via Messages", False),
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
