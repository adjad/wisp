"""User-facing assistant read contracts; synthetic state and tool execution only."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import json

import pytest

import service.tools  # noqa: F401
from service.router import router as R
from service.tools import email_tools as E


@pytest.fixture(autouse=True)
def offline_retrieval(monkeypatch):
    monkeypatch.setattr(R, '_semantic_core', AsyncMock(return_value=[
        'get_upcoming', 'summarize_emails', 'summarize_messages', 'search_notes']))


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt', [
    'what is my plan for tomorrow', 'what is my plan for tommorow',
    "what's my plan tomorrow?", 'show me my plan for tomorrow',
])
async def test_existing_personal_plan_reads_tomorrow_commitments(prompt):
    d = await R.route(prompt)
    assert d.needs_tools and d.verified_results_only
    assert d.direct_calls == [('get_upcoming', {'period': 'tomorrow', 'calendar_only': False})]
    assert {'web_search', 'recall', 'run_shell'} <= d.forbidden_tools


@pytest.mark.asyncio
@pytest.mark.parametrize('period', ['tomorrow', 'tommorow', 'tommorrow', 'tomorow', 'tommrow', 'tmrow', 'tmrw'])
async def test_priority_spelling_does_not_drop_deadline_sources(period):
    d = await R.route('what should I do ' + period)
    [(name, args)] = d.direct_calls
    assert name == 'collect_task_evidence' and args['period'] == 'tomorrow'
    assert all(args['include_' + source] for source in ('calendar', 'reminders', 'email', 'messages', 'notes'))
    assert d.strict_read_limits == {'collect_task_evidence': 1}


@pytest.mark.asyncio
@pytest.mark.parametrize('source,tool', [('email', 'view_emails'), ('messages', 'view_messages'), ('calendar', 'get_upcoming'), ('notes', 'search_notes'), ('reminders', 'search_reminders')])
@pytest.mark.parametrize('template', ["don't read {source}; what should I do tomorrow?", "what should I do tomorrow; don't read {source}", "don't read {source}, what should I do tomorrow?"])
async def test_negative_sources_survive_priority_clause_merging(source, tool, template):
    d = await R.route(template.format(source=source))
    [(name, args)] = d.direct_calls
    assert name == 'collect_task_evidence' and args['period'] == 'tomorrow'
    assert args['include_' + source] is False
    assert any(args['include_' + other] for other in ('calendar', 'reminders', 'email', 'messages', 'notes') if other != source)
    assert tool in d.forbidden_tools
    assert {'run_shell', 'run_applescript', 'daily_brief'} <= d.forbidden_tools
    assert set(d.tool_subset) == {'collect_task_evidence'}


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt', [
    'without reading my inbox, what should I do tomorrow?',
    'what should I do tomorrow without reading my inbox',
])
async def test_without_reading_inbox_keeps_bound_remaining_priority_sources(prompt):
    d = await R.route(prompt)
    assert d.direct_calls[0][0] == 'collect_task_evidence'
    assert d.direct_calls[0][1]['include_email'] is False
    assert {'view_emails', 'daily_brief', 'run_shell', 'run_applescript'} <= d.forbidden_tools


@pytest.mark.asyncio
async def test_reminder_denial_narrows_existing_agenda_direct_call():
    d = await R.route("don't read reminders; what is my plan for tomorrow?")
    assert d.direct_calls == [('get_upcoming', {'period': 'tomorrow', 'calendar_only': True})]
    assert d.tool_argument_bindings['get_upcoming']['calendar_only'] is True
    assert 'daily_brief' in d.forbidden_tools


@pytest.mark.asyncio
async def test_bound_daily_digest_keeps_compatible_sources(monkeypatch):
    from service.tools import everyday
    calendar = AsyncMock(side_effect=AssertionError('excluded Calendar read'))
    reminders = AsyncMock(return_value='Synthetic reminder evidence')
    activity = AsyncMock(return_value='Synthetic communication evidence')
    monkeypatch.setattr(everyday, '_daily_calendar', calendar)
    monkeypatch.setattr(everyday, '_daily_reminders', reminders)
    monkeypatch.setattr(everyday, '_daily_activity', activity)
    d = await R.route('Daily digest next week, no calendar and weather')
    [(name, args)] = d.direct_calls
    assert name == 'daily_brief' and args['include_calendar'] is False
    await everyday.daily_brief(**args)
    calendar.assert_not_called()
    reminders.assert_awaited_once()
    assert activity.await_count == 2


@pytest.mark.asyncio
async def test_negative_source_guard_also_covers_nonpriority_routes():
    d = await R.route("don't read email; show my unread messages")
    assert {'view_emails', 'summarize_emails', 'triage_inbox', 'collect_task_evidence'} <= d.forbidden_tools
    assert not set(d.tool_subset or ()) & {'view_emails', 'summarize_emails', 'triage_inbox'}


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt,sources', [
    ('what needs attention on email/messages', {'email', 'messages'}),
    ('what needs my attention in email and messages?', {'email', 'messages'}),
    ('which emails are important?', {'email'}),
    ('what needs attention in my messages?', {'messages'}),
    ('which texts should I reply to?', {'messages'}),
    ('what should I reply to first?', {'email', 'messages'}),
])
async def test_attention_reads_current_bodies_for_every_selected_source(prompt, sources):
    d = await R.route(prompt)
    [(name, args)] = d.direct_calls
    assert name == 'collect_task_evidence'
    selected = {source for source in ('calendar', 'reminders', 'email', 'messages', 'notes') if args['include_' + source]}
    assert selected == sources
    assert set(d.tool_subset) == {'collect_task_evidence'}
    assert d.verified_results_only
    assert 'promotions' in d.resolved_request.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt', ['hello', 'thanks', 'write a fictional day plan', 'explain how to prioritize email'])
async def test_general_conversation_does_not_read_private_sources(prompt):
    assert not (await R.route(prompt)).needs_tools


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt,tools', [
    ('Save a knowledge note titled Project:\nDo not read my calendar; text Mom my availability.', {'save_knowledge'}),
    ('Save a style example titled Do not read my email:\nKeep replies brief.', {'save_knowledge'}),
    ('Update knowledge ' + 'a' * 32 + ' titled Project:\nDo not read email; summarize messages.',
     {'read_knowledge', 'edit_knowledge'}),
    ('Save email message-id in account account-id as a knowledge note titled Do not read my calendar',
     {'save_knowledge'}),
])
async def test_knowledge_literal_fields_cannot_change_source_policy(prompt, tools):
    d = await R.route(prompt)
    assert set(d.tool_subset) == tools
    assert not d.direct_calls
    assert {'view_emails', 'get_upcoming', 'send_message', 'run_shell'} <= d.forbidden_tools
    if ':\n' in prompt:
        effect = 'edit_knowledge' if 'edit_knowledge' in tools else 'save_knowledge'
        assert d.tool_argument_bindings[effect]['text'] == prompt.split(':\n', 1)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', [
    "Don't read email;", 'Without reading my inbox,',
    'Without reading my messages,', "Don't read reminders;",
    'Without reading my calendar,', "Don't read notes;",
    'Without reading my inbox', 'Without reading my inbox and',
    'Without reading my inbox but', "Don't read my messages and",
    ' Without reading my inbox', '\nWithout reading my inbox',
    '\tWithout reading my inbox and',
])
async def test_actual_source_denial_before_knowledge_looking_text_does_not_gain_authority(prefix):
    d = await R.route(prefix + " save a knowledge note titled Project:\nLiteral text.")
    assert not d.tool_subset and not d.direct_calls
    assert not d.needs_tools and not d.expect_tool_first
    assert not d.force_first_tool and not d.required_tool_groups
    assert {'view_emails', 'view_messages', 'search_notes', 'save_knowledge', 'run_shell'} <= d.forbidden_tools


@pytest.mark.asyncio
async def test_unsupported_knowledge_compound_accepts_first_clarification(monkeypatch):
    from service.agent import loop

    class Client:
        def __init__(self):
            self.requests = []

        async def ensure_only(self, *args, **kwargs):
            pass

        async def stream_events(self, model, messages, **kwargs):
            self.requests.append(kwargs)
            yield {'kind': 'final', 'message': {'role': 'assistant', 'content':
                'Please provide the complete knowledge note instruction, keeping the inbox restriction.'}}

    async def emit(_event):
        pass

    execute = AsyncMock(side_effect=AssertionError('clarification must not execute tools'))
    monkeypatch.setattr(loop, 'run_tool', execute)
    d = await R.route('Without reading my inbox, save a knowledge note titled Project:\nLiteral text.')
    client = Client()
    answer = await loop.run_agent(client, 'Agents-A1-4B-oQe6',
        [{'role': 'user', 'content': d.resolved_request}], emit, AsyncMock(),
        tools=d.tool_subset, direct_calls=d.direct_calls, expect_tool_first=d.expect_tool_first,
        required_tool_groups=d.required_tool_groups, forbidden_tools=d.forbidden_tools,
        include_memory_context=False, debug=False, max_steps=3)
    assert answer.startswith('Please provide the complete knowledge note instruction')
    assert len(client.requests) == 1
    assert client.requests[0]['tool_choice'] == 'auto'
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_priorities_do_not_become_current_web_search():
    d = await R.route('what needs me today?')
    assert d.direct_calls[0][0] == 'collect_task_evidence'
    assert 'web_search' in d.forbidden_tools


@pytest.mark.asyncio
async def test_empty_messages_does_not_skip_requested_mail_bodies(monkeypatch):
    from service.tools import task_evidence as T, imessage_tools as I
    header = {**row(), 'to': ['self@synthetic.invalid'], 'body': 'Please review the contract.'}
    snapshot = SimpleNamespace(generation=1, rows=(header,), accounts=('Synthetic',),
                               synced_at=100, raw_digest='synthetic')
    mail = Mock(return_value=snapshot)
    messages = Mock(return_value={'state': 'synthetic bounded snapshot', 'records': [], 'coverage': {}})
    monkeypatch.setattr(E, 'raw_email_snapshot', mail)
    monkeypatch.setattr(I, 'structured_messages_snapshot', messages)
    monkeypatch.setattr(T, '_native_source', Mock(side_effect=AssertionError('unrequested source')))
    d = await R.route('what needs attention on email/messages')
    payload = json.loads(await T.collect_task_evidence(**d.direct_calls[0][1]))
    assert set(payload['sources']) == {'email', 'messages'}
    assert payload['sources']['email']['records'][0]['body'] == header['body']
    assert payload['sources']['messages']['records'] == []
    mail.assert_called_once()
    messages.assert_called_once()


@pytest.mark.asyncio
async def test_excluded_mail_is_never_read_by_bound_collector(monkeypatch):
    from service.tools import task_evidence as T, imessage_tools as I
    mail = Mock(side_effect=AssertionError('excluded Mail read'))
    monkeypatch.setattr(E, 'raw_email_snapshot', mail)
    monkeypatch.setattr(I, 'structured_messages_snapshot', lambda: {
        'state': 'synthetic bounded snapshot', 'records': [], 'coverage': {}})
    d = await R.route("don't read email; what needs attention on email/messages")
    payload = json.loads(await T.collect_task_evidence(**d.direct_calls[0][1]))
    assert set(payload['sources']) == {'messages'} and 'email' in payload['excluded_sources']
    mail.assert_not_called()


@pytest.mark.asyncio
async def test_direct_evidence_read_accepts_first_grounded_answer(monkeypatch):
    from service.agent import loop
    from service.tools.registry import REGISTRY

    class Client:
        def __init__(self):
            self.requests = []

        async def ensure_only(self, *args, **kwargs):
            pass

        async def stream_events(self, model, messages, **kwargs):
            self.requests.append((messages, kwargs))
            yield {'kind': 'final', 'message': {'role': 'assistant', 'content':
                'Sam asks for contract comments by tomorrow at 3 PM. The showcase invitations are promotional.'}}

    async def emit(_event):
        pass

    evidence = json.dumps({'sources': {'email': {'records': [{'sender': 'Sam',
        'message_id': 'synthetic-request', 'body': 'Please review the contract by tomorrow at 3 PM.'}]}}})
    execute = AsyncMock(return_value=evidence)
    monkeypatch.setattr(loop, 'run_tool', execute)
    d = await R.route('what needs attention on email/messages')
    client = Client()
    answer = await loop.run_agent(client, 'Agents-A1-4B-oQe6',
        [{'role': 'user', 'content': d.resolved_request}], emit, AsyncMock(),
        tools=d.tool_subset, direct_calls=d.direct_calls, expect_tool_first=d.expect_tool_first,
        required_tool_groups=d.required_tool_groups, forbidden_tools=d.forbidden_tools,
        tool_argument_bindings=d.tool_argument_bindings, strict_read_limits=d.strict_read_limits,
        multi_round=d.multi_round, narration_after=d.narration_after,
        include_memory_context=False, debug=False, max_steps=3)
    assert 'Sam asks for contract comments' in answer
    assert len(client.requests) == 1
    assert client.requests[0][1]['tool_choice'] == 'auto'
    execute.assert_awaited_once_with(REGISTRY['collect_task_evidence'], d.direct_calls[0][1])


def row(subject='A special invitation for you', sender='Taylor', message='m1', account='a1'):
    return dict(ts=100, account='Synthetic', account_id=account, message_id=message,
                native_id='', sender=sender, sender_address='taylor@events.example',
                subject=subject, unread=True)


def snapshot_for(header, body):
    return SimpleNamespace(generation=1, rows=({**header, 'body': body},))


@pytest.mark.parametrize('subject,body', [
    ('Last chance: RSVP by tomorrow', 'Register for our showcase. Manage your email preferences below.'),
    ('A special invitation for you', 'Product showcase. Unsubscribe from this mailing list below.'),
])
def test_promo_body_changes_digest_category_without_exposing_body(monkeypatch, subject, body):
    header = row(subject)
    snapshot = snapshot_for(header, body)
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert E.digest_category(header) == 'attention'  # header-only evidence is ambiguous
    digest = E.sender_digest([header], 'synthetic')
    assert 'Needs your attention' not in digest
    assert body not in digest
    assert '_current_list_footer' not in header


@pytest.mark.parametrize('changed', [dict(account_id='another-account'), dict(message_id='another-message'),
                                    dict(account='Another label'), dict(subject='Another subject')])
def test_promo_evidence_cannot_cross_account_or_message_identity(monkeypatch, changed):
    header = row()
    snapshot = snapshot_for({**header, **changed}, 'Product showcase. Manage your email preferences.')
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert '1 needs attention' in E.sender_digest([header], 'synthetic')


@pytest.mark.parametrize('snapshot', [None, SimpleNamespace(rows=())])
def test_missing_body_generation_preserves_header_evidence(monkeypatch, snapshot):
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert '1 needs attention' in E.sender_digest([row()], 'synthetic')


def test_current_promo_body_is_not_a_workspace_obligation(monkeypatch):
    from service.assistant.daily_workspace import important_email_rows
    header = row()
    snapshot = snapshot_for(header, 'Product showcase. Unsubscribe from this mailing list below.')
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert important_email_rows(snapshot) == []


def test_body_generation_change_drops_classification_evidence(monkeypatch):
    header = row()
    snapshot = snapshot_for(header, 'Product showcase. Manage your email preferences.')
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: None if kwargs else snapshot)
    assert '1 needs attention' in E.sender_digest([header], 'synthetic')


@pytest.mark.parametrize('subject', ['Security alert: verify your account', 'Payment failed'])
def test_protected_alert_still_surfaces_with_a_list_footer(monkeypatch, subject):
    header = row(subject, sender='Shop Newsletter')
    snapshot = snapshot_for(header, 'Manage your email preferences. A problem with your account needs review.')
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert '1 needs attention' in E.sender_digest([header], 'synthetic')


def test_quoted_list_footer_does_not_demote_a_real_invitation(monkeypatch):
    header = row()
    snapshot = snapshot_for(header, 'Can you come to dinner?\nOn Monday Taylor wrote:\nManage your email preferences.')
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert '1 needs attention' in E.sender_digest([header], 'synthetic')


@pytest.mark.parametrize('body', [
    'Could you help me unsubscribe from this mailing list?',
    'Can you explain how to manage your email preferences?',
    'Can you explain why you are receiving this notice?',
])
def test_footer_phrase_inside_personal_request_does_not_hide_it(monkeypatch, body):
    header = row('Can you help?')
    snapshot = snapshot_for(header, body)
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert '1 needs attention' in E.sender_digest([header], 'synthetic')


@pytest.mark.parametrize('separator', ['. ', '.', '\n'])
def test_promotion_footer_can_start_a_sentence_or_line(monkeypatch, separator):
    header = row()
    snapshot = snapshot_for(header, 'Product showcase' + separator + 'Unsubscribe from this mailing list below.')
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert 'Needs your attention' not in E.sender_digest([header], 'synthetic')


@pytest.mark.parametrize('subject', ['Assignment 1 due tonight', 'Midterm exam rescheduled to Oct 8',
                                    'PA 1 and the next reading assignment posted', 'Office Hours CANCELLED'])
def test_course_alert_is_not_hidden_by_normal_preferences_footer(monkeypatch, subject):
    from service.assistant.daily_workspace import important_email_rows
    header = {**row(subject, sender='Canvas'), 'sender_address': 'notifications@instructure.com'}
    snapshot = snapshot_for(header, subject + '.\nManage your email preferences below.')
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert '1 needs attention' in E.sender_digest([header], 'synthetic')
    assert important_email_rows(snapshot)


def test_course_newsletter_is_not_exempted_as_a_course_alert(monkeypatch):
    header = row('Course newsletter: new offers', sender='Canvas Newsletter')
    snapshot = snapshot_for(header, 'Product showcase.\nManage your email preferences below.')
    monkeypatch.setattr(E, 'raw_email_snapshot', lambda **kwargs: snapshot)
    assert 'Needs your attention' not in E.sender_digest([header], 'synthetic')
