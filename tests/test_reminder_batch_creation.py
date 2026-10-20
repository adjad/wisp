"""Reported creation failures; synthetic stores and native receipts only."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import service.tools
from service.tasks.compiler import compile_task
from service.tasks.models import TaskPlan
from service.tasks.planner import plan_task, InvalidTaskPlan
from service.tasks.executor import execute_task
from service.safety.policy import Decision, Tier
from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.engine import prepare_task_turn, finish_task


NOW = datetime(2026, 10, 19, 16, 9)
REQUEST = ('set a reminder on october 20th for vicor earnings report '
           'and another on the 28th for microsoft and google earnings report')


def ready(text=REQUEST):
    plan = compile_task(text, now=NOW)
    assert plan is not None
    plan_task(plan)
    plan.status = 'running'
    return plan


def test_reported_request_preserves_both_subjects_and_exact_dates():
    plan = ready()
    assert [(s.args['title'], s.args['when_iso']) for s in plan.steps] == [
        ('vicor earnings report', '2026-10-20T09:00'),
        ('microsoft and google earnings report', '2026-10-28T09:00')]
    assert len({s.id for s in plan.steps}) == 2
    assert all(s.tool == 'add_reminder' and s.effect and s.max_calls == 1 for s in plan.steps)
    assert TaskPlan.from_dict(plan.to_dict()).to_dict() == plan.to_dict()


def test_all_day_is_a_date_not_a_midnight_alert():
    plan = ready(REQUEST.replace('a reminder', 'all-day reminders', 1))
    assert [s.args for s in plan.steps] == [
        {'title': 'vicor earnings report', 'when_iso': '2026-10-20', 'kind': 'reminder', 'all_day': True},
        {'title': 'microsoft and google earnings report', 'when_iso': '2026-10-28', 'kind': 'reminder', 'all_day': True}]


@pytest.mark.parametrize('suffix', [
    ' and email Sam about them', ' or another on the 29th for something else',
    ' if the market closes higher', '; delete my old reminders',
])
def test_additional_unresolved_actions_never_become_batch_effects(suffix):
    plan = compile_task(REQUEST + suffix, now=NOW)
    assert plan is None or plan.status != 'ready'


def test_invalid_second_item_prevents_every_effect():
    plan = compile_task(REQUEST, now=NOW)
    assert plan is not None
    plan.parameters['items'].value[1]['when_iso'] = '2026-02-30T09:00'
    with pytest.raises(InvalidTaskPlan):
        plan_task(plan)
    assert not plan.steps


@pytest.mark.asyncio
async def test_every_created_item_requires_its_own_readback(monkeypatch):
    from service.tasks import executor as X
    plan = ready()
    monkeypatch.setattr(X, 'decide', lambda *a, **k: Decision(Tier.ALLOW, 'synthetic'))
    rows = []
    async def run(tool, args):
        # Second tool falsely reports success without adding its own row.
        if not rows:
            rows.append({'source': 'manual', 'title': args['title'],
                         'when_ts': datetime.fromisoformat(args['when_iso']).timestamp()})
        return 'Reminder set: ' + args['title']
    execute = AsyncMock(side_effect=run)
    monkeypatch.setattr(X, 'run_tool', execute)
    execution = await execute_task(plan, AsyncMock(), SimpleNamespace(confirm=AsyncMock()), now=NOW,
        assistant_store=SimpleNamespace(active_between=lambda *a, **k: rows))
    assert execution.status == 'failed'
    assert [r['status'] for r in execution.tool_results] == ['succeeded', 'failed']
    assert 'vicor earnings report' in execution.response
    assert 'microsoft and google earnings report' in execution.response


@pytest.mark.asyncio
async def test_dry_run_plans_both_items_without_any_claim_or_tool(monkeypatch):
    from service.tasks import executor as X
    plan = ready()
    execute = AsyncMock(side_effect=AssertionError('dry run wrote a reminder'))
    monkeypatch.setattr(X, 'run_tool', execute)
    execution = await execute_task(plan, AsyncMock(), SimpleNamespace(confirm=AsyncMock()), now=NOW, test_mode=True)
    assert execution.status == 'planned'
    assert len(execution.tool_calls) == 2
    assert len(execution.tool_results) == 2
    assert not plan.claimed_calls
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_clarification_retains_the_complete_request(monkeypatch):
    from service.router import router as R
    monkeypatch.setattr(R, '_semantic_core', AsyncMock(return_value=[]))
    text = 'remind me to call mom and do something with my files'
    decision = await R.route(text)
    assert not decision.tool_subset
    assert text in decision.resolved_request


def test_all_day_native_receipt_must_prove_the_requested_date():
    from service.assistant.verified_reminders import validate_payload, validate_result
    payload = {'type': 'create_reminder', 'action_id': 'fixture-date', 'title': 'Review report',
               'all_day': True, 'due_date': '2026-10-20', 'commitment_kind': 'reminder'}
    validate_payload('create_reminder', payload)
    receipt = {'ok': True, 'status': 'succeeded', 'error': '', 'source_id': 'fixture:1',
               'title': 'Review report', 'all_day': True, 'due_date': '2026-10-20'}
    assert validate_result('create_reminder', payload, receipt) == receipt
    with pytest.raises(ValueError):
        validate_result('create_reminder', payload, {**receipt, 'due_date': '2026-10-21'})


@pytest.fixture
def stores(tmp_path):
    sessions, assistant = SessionStore(tmp_path / 'sessions.db'), AssistantStore(tmp_path / 'assistant.db')
    try:
        yield sessions, assistant, sessions.create_session()
    finally:
        sessions._db.close(); assistant._db.close()


@pytest.mark.parametrize('text', [
    'set a reminder on february 30th for review and another on the 28th for draft',
    'set all-day reminders on october 20th at 3pm for review and another on the 28th for draft',
    'set a reminder on the 28th for review',
    'set a reminder on october 20th for review and another on the 40th for draft',
    'set a reminder on october 20th for review and another on the 20th for review',
])
def test_ambiguous_batch_has_no_executable_step(stores, text):
    sessions, assistant, sid = stores
    turn = prepare_task_turn(sessions, sid, text, assistant_store=assistant, now=NOW)
    assert turn is not None and not turn.executable
    assert not turn.plan.steps
    assert turn.plan.original_request == text


def test_ellipsis_inherits_month_and_year_but_not_clock():
    plan = ready('set a reminder on october 20th at 3pm for review and another on the 28th for draft')
    assert [s.args['when_iso'] for s in plan.steps] == ['2026-10-20T15:00', '2026-10-28T09:00']
    plan = compile_task('set a reminder on january 20th for review and another on the 28th for draft', now=NOW)
    assert [s.args['when_iso'] for s in plan_task(plan)] == ['2027-01-20T09:00', '2027-01-28T09:00']


def test_literal_conjunctions_and_action_words_remain_only_title_data():
    plan = ready('set a reminder on october 20th for "email Sam and another on the 28th for a report"')
    assert len(plan.steps) == 1
    assert plan.steps[0].args['title'] == 'email Sam and another on the 28th for a report'
    assert plan.steps[0].tool == 'add_reminder'


@pytest.mark.parametrize('text', [
    'If the report is ready, ' + REQUEST,
    'Explain the request "' + REQUEST + '"',
    'Do not ' + REQUEST,
    'Say "' + REQUEST + '"',
])
def test_mentions_and_conditions_grant_no_typed_creation(text):
    plan = compile_task(text, now=NOW)
    assert plan is None or plan.status != 'ready'


def test_reassertion_and_all_day_answer_never_become_an_unrelated_subject(stores):
    sessions, assistant, sid = stores
    initial = prepare_task_turn(sessions, sid, 'I want you to set a reminder', assistant_store=assistant, now=NOW)
    assert initial is not None and not initial.executable and 'subject' in initial.plan.missing_slots
    answer = prepare_task_turn(sessions, sid, 'keep it all day long', assistant_store=assistant, now=NOW)
    assert answer is not None and not answer.executable
    assert not answer.plan.subject.value
    assert answer.plan.parameters['all_day'].value is True


@pytest.mark.parametrize('text', [
    'create an all-day reminder tomorrow to review report',
    'remind me to review report tomorrow all day',
])
def test_explicit_single_all_day_forms(text):
    plan = ready(text)
    assert plan.steps[0].args == {'title': 'review report', 'when_iso': '2026-10-20', 'kind': 'reminder', 'all_day': True}


@pytest.mark.parametrize('answer', ['9am', 'actually 9am', 'like 9am tomorrow', 'I mean today', 'keep it all day long'])
def test_post_batch_correction_requires_an_item_not_the_first_title(stores, answer):
    sessions, assistant, sid = stores
    turn = prepare_task_turn(sessions, sid, REQUEST, assistant_store=assistant, now=NOW)
    assert turn.executable
    finish_task(sessions, sid, turn.plan, status='completed', result='synthetic completion')
    next_turn = prepare_task_turn(sessions, sid, answer, assistant_store=assistant, now=NOW)
    assert next_turn is not None and not next_turn.executable
    assert next_turn.event == 'batch_item_clarification'
    assert 'Which reminder' in next_turn.response


@pytest.mark.asyncio
async def test_complete_batch_reports_each_verified_item_and_cannot_replay(stores, monkeypatch):
    from service.tasks import executor as X
    sessions, assistant, sid = stores
    turn = prepare_task_turn(sessions, sid, REQUEST, assistant_store=assistant, now=NOW)
    monkeypatch.setattr(X, 'decide', lambda *a, **k: Decision(Tier.ALLOW, 'synthetic'))
    async def run(tool, args):
        assistant.add_manual(args['title'], datetime.fromisoformat(args['when_iso']).timestamp())
        return 'Reminder set: ' + args['title']
    execute = AsyncMock(side_effect=run); monkeypatch.setattr(X, 'run_tool', execute)
    claim = lambda p, call_id: sessions.claim_effect_call(p.id, call_id, revision=p.revision)
    result = await execute_task(turn.plan, AsyncMock(), SimpleNamespace(confirm=AsyncMock()), now=NOW,
        assistant_store=assistant, on_claim=claim)
    assert result.status == 'completed' and execute.await_count == 2
    assert all(s.args['title'] in result.response for s in turn.plan.steps)
    restored = TaskPlan.from_dict(sessions.active_task(sid))
    retry = await execute_task(restored, AsyncMock(), SimpleNamespace(confirm=AsyncMock()), now=NOW,
        assistant_store=assistant, on_claim=claim)
    assert retry.status == 'failed' and not retry.finalize and execute.await_count == 2


@pytest.mark.asyncio
async def test_partial_failure_stops_before_third_and_reports_all_items(stores, monkeypatch):
    from service.tasks import executor as X
    sessions, assistant, sid = stores
    text = REQUEST + ' and another on the 29th for final review'
    turn = prepare_task_turn(sessions, sid, text, assistant_store=assistant, now=NOW)
    monkeypatch.setattr(X, 'decide', lambda *a, **k: Decision(Tier.ALLOW, 'synthetic'))
    async def run(tool, args):
        if args['title'] == 'vicor earnings report':
            assistant.add_manual(args['title'], datetime.fromisoformat(args['when_iso']).timestamp())
            return 'Reminder set: ' + args['title']
        return '(error: native outcome unknown; check before retrying.)'
    execute = AsyncMock(side_effect=run); monkeypatch.setattr(X, 'run_tool', execute)
    result = await execute_task(turn.plan, AsyncMock(), SimpleNamespace(confirm=AsyncMock()), now=NOW,
        assistant_store=assistant)
    assert result.status == 'failed' and execute.await_count == 2
    assert 'Reminder set: vicor earnings report' in result.response
    assert 'Not confirmed: microsoft and google earnings report' in result.response
    assert 'Not attempted: final review' in result.response


@pytest.mark.asyncio
async def test_cancellation_can_stop_remaining_items_without_undoing_first(stores, monkeypatch):
    from service.tasks import executor as X
    sessions, assistant, sid = stores
    turn = prepare_task_turn(sessions, sid, REQUEST, assistant_store=assistant, now=NOW)
    monkeypatch.setattr(X, 'decide', lambda *a, **k: Decision(Tier.ALLOW, 'synthetic'))
    async def run(tool, args):
        assistant.add_manual(args['title'], datetime.fromisoformat(args['when_iso']).timestamp())
        cancelled = prepare_task_turn(sessions, sid, 'cancel', assistant_store=assistant, now=NOW)
        assert cancelled.event == 'cancelled' and 'not undone' in cancelled.response
        return 'Reminder set: ' + args['title']
    execute = AsyncMock(side_effect=run); monkeypatch.setattr(X, 'run_tool', execute)
    result = await execute_task(turn.plan, AsyncMock(), SimpleNamespace(confirm=AsyncMock()), now=NOW,
        assistant_store=assistant,
        on_claim=lambda p, c: sessions.claim_effect_call(p.id, c, revision=p.revision))
    assert execute.await_count == 1
    assert result.status == 'failed' and not result.finalize
    assert sessions.latest_task(sid)['status'] == 'cancelled'


def test_all_day_receipt_persists_date_identity_and_never_queues_a_midnight_alert(stores):
    from service.assistant.reminders import due_reminders
    sessions, assistant, sid = stores
    action = {'type': 'create_reminder', 'action_id': 'fixture-date', 'title': 'Review report',
              'all_day': True, 'due_date': '2026-10-20', 'commitment_kind': 'reminder'}
    receipt = {'ok': True, 'status': 'succeeded', 'error': '', 'source_id': 'fixture:date',
               'title': action['title'], 'all_day': True, 'due_date': action['due_date']}
    import time
    row = assistant.enqueue_event(action, target={'type': 'verified_reminder'},
        dedupe_key='action:' + action['action_id'], expires_at=time.time() + 45)
    claim = assistant.claim_calendar_action(row['id'], 'create_reminder', action['action_id'], action)
    assert assistant.complete_calendar_action(row['id'], 'create_reminder', claim['claim_token'], receipt)
    from service.assistant.verified_reminders import due_timestamp
    when = due_timestamp(receipt)
    rows = assistant.active_between(datetime(2026, 10, 20).timestamp(), datetime(2026, 10, 21).timestamp(), include_stale=True)
    assert len(rows) == 1 and rows[0]['all_day']
    assert due_reminders(assistant, now=when) == []
    assert due_reminders(assistant, now=datetime(2026, 10, 20).timestamp()) == []
    previous = assistant.event(row['id'])
    assert assistant.reminder_create_retry_state(previous) == 'present'
    assistant.add_manual(action['title'], when)
    assert len(assistant.active_between(when - 1, when + 1, include_stale=True)) == 2
