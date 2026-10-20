"""Outer speech acts own authority; quoted words and nouns do not create it.

All entrypoint checks use disposable stores, scripted inference, denied
confirmations and inert tools. They establish admission, not model quality.
"""
import copy
from dataclasses import replace
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
]
NON_OUTBOUND = [
    'How can I tell whether an email needs a reply?',
    'What messages did I send yesterday?',
    'What emails did I send yesterday?',
    'What did I send yesterday?',
    'Summarize my unread messages without reading email.',
    'Show me my email and calendar today',
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
