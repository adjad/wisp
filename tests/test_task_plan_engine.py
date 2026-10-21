"""Quoted completion commands resolve an exact synthetic reminder target."""
from datetime import datetime, timedelta
import asyncio
import socket
import subprocess
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.engine import prepare_task_turn
from service.tasks.executor import execute_task
from service.tools.registry import REGISTRY


@pytest.fixture(autouse=True)
def inert(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No native, network or inference for quoted-target controls')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


@pytest.fixture
def stores(tmp_path):
    sessions = SessionStore(tmp_path / 'sessions.db')
    reminders = AssistantStore(tmp_path / 'reminders.db')
    yield sessions, reminders, datetime(2026, 10, 20, 12)
    sessions._db.close()
    reminders._db.close()


def test_mark_reminder_titled_quoted_name_resolves_actual_target(tmp_path):
    now = datetime(2026, 10, 20, 12)
    sessions = SessionStore(tmp_path / 'sessions.db')
    reminders = AssistantStore(tmp_path / 'reminders.db')
    try:
        title = 'Wisp fictional QA reminder'
        wanted = reminders.add_manual(title, (now + timedelta(days=1)).timestamp())
        reminders.add_manual('Other fictional reminder', (now + timedelta(days=1)).timestamp())
        sid = sessions.create_session()
        turn = prepare_task_turn(sessions, sid, f'Mark the reminder titled "{title}" complete',
                                 assistant_store=reminders, now=now)
        assert turn is not None and turn.executable, turn
        assert turn.plan.target.value == title
        assert turn.plan.steps[0].tool == 'complete_reminder'
        assert turn.plan.steps[0].args == {'title': title, 'expected_id': wanted['id']}
        assert all(row['status'] == 'active' for row in reminders.active_between(
            now.timestamp(), (now + timedelta(days=2)).timestamp()))
    finally:
        sessions._db.close()
        reminders._db.close()


@pytest.mark.parametrize('quotes,title,label', [
    ('""', 'Wisp fictional QA reminder', 'titled'),
    ('“”', 'all overdue requests', 'named'),
    ("''", 'Review blue form.', 'called'),
    ('‘’', 'done? Complete! Review blue form.', 'titled'),
    ('``', 'Review and send the blue form', 'titled'),
    ('""', "don't mark this as done", 'titled'),
    ('""', 'Review blue form and email Mom', 'titled'),
])
def test_named_quoted_completion_keeps_literal_title(stores, quotes, title, label):
    sessions, reminders, now = stores
    row = reminders.add_manual(title, (now + timedelta(days=1)).timestamp())
    sid = sessions.create_session()
    prompt = f'Please mark my reminder {label} {quotes[0]}{title}{quotes[1]} as done.'
    turn = prepare_task_turn(sessions, sid, prompt, assistant_store=reminders, now=now)
    assert turn and turn.executable
    assert turn.plan.target.value == title
    assert turn.plan.steps[0].args == {'title': title, 'expected_id': row['id']}


@pytest.mark.parametrize('prompt', [
    'Do not mark the reminder titled "Blue form" complete',
    'Explain "Mark the reminder titled Blue form complete"',
    'Mark the reminder titled "Blue form" complete and email Mom',
    'Mark the reminder titled "Blue form" complete; delete the other reminder',
    'Mark the reminder titled "Blue form" complete if I have time',
    'Mark the reminder titled "Blue form" complete tomorrow',
    'Mark the reminder titled "Blue form complete',
    'Mark the reminder titled "Blue "form"" complete',
    'Mark the reminder titled "Blue form" complete "Other reminder"',
])
def test_quoted_completion_does_not_partially_resolve_outer_prose(stores, prompt):
    sessions, reminders, now = stores
    row = reminders.add_manual('Blue form', (now + timedelta(days=1)).timestamp())
    sid = sessions.create_session()
    turn = prepare_task_turn(sessions, sid, prompt, assistant_store=reminders, now=now)
    assert turn is None or not turn.executable
    assert reminders.get(row['id'])['status'] == 'active'


def test_existing_ambiguous_completion_still_accepts_a_genuine_title_followup(stores):
    sessions, reminders, now = stores
    wanted = reminders.add_manual('Dentist cleaning', (now + timedelta(days=1)).timestamp())
    reminders.add_manual('Call dentist', (now + timedelta(days=1)).timestamp())
    sid = sessions.create_session()
    first = prepare_task_turn(sessions, sid, 'mark my dentist reminder as done', assistant_store=reminders, now=now)
    assert first and first.event == 'target_ambiguous' and not first.executable
    second = prepare_task_turn(sessions, sid, 'Dentist cleaning', assistant_store=reminders, now=now)
    assert second and second.executable and second.plan.id == first.plan.id
    assert second.plan.steps[0].args == {'title': 'Dentist cleaning', 'expected_id': wanted['id']}


@pytest.mark.parametrize('change_state', [True, False])
def test_quoted_completion_execution_checks_exact_state_not_just_success_text(stores, monkeypatch, change_state):
    sessions, reminders, now = stores
    wanted = reminders.add_manual('Blue form', (now + timedelta(days=1)).timestamp())
    other = reminders.add_manual('Other form', (now + timedelta(days=1)).timestamp())
    turn = prepare_task_turn(sessions, sessions.create_session(), 'Mark the reminder titled "Blue form" complete',
                             assistant_store=reminders, now=now)
    assert turn and turn.executable
    calls = []
    async def complete(title, expected_id):
        calls.append((title, expected_id))
        if change_state:
            reminders.set_status(expected_id, 'done')
        return 'Marked Blue form done.'
    monkeypatch.setitem(REGISTRY, 'complete_reminder', replace(REGISTRY['complete_reminder'], func=complete))
    from service.safety import policy
    monkeypatch.setattr(policy, '_READ_ONLY', False)
    result = asyncio.run(execute_task(turn.plan, AsyncMock(), type('Approver', (), {'confirm': AsyncMock(return_value=True)})(),
                                     assistant_store=reminders, now=now))
    assert calls == [('Blue form', wanted['id'])]
    assert result.status == ('completed' if change_state else 'failed')
    assert reminders.get(other['id'])['status'] == 'active'
    assert reminders.get(wanted['id'])['status'] == ('done' if change_state else 'active')
