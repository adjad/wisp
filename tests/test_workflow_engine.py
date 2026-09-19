"""Strict contracts for persistent, typed outbound workflows."""
from __future__ import annotations

import tempfile
from pathlib import Path

from service.memory.store import SessionStore
from service.workflows.compiler import compile_decision, compile_new
from service.workflows.engine import finish_workflow, prepare_turn


def _store():
    temp = tempfile.TemporaryDirectory()
    return temp, SessionStore(Path(temp.name) / "sessions.db")


def _groups(decision) -> list[set[str]]:
    return [set(group) for group in decision.required_tool_groups]


def test_complete_calendar_message_compiles_exact_ordered_plan():
    plan = compile_new("Send Mom a summary of my calendar tomorrow via Messages")
    assert plan is not None
    assert plan.status == "ready"
    assert plan.sources == ["calendar"]
    assert plan.source_args == {"calendar": {"period": "tomorrow"}}
    assert plan.recipient == "Mom"
    assert plan.channel == "messages"

    decision = compile_decision(plan)
    assert decision.tool_subset == ["get_upcoming", "lookup_contact", "send_message"]
    assert decision.direct_calls == [
        ("get_upcoming", {"period": "tomorrow"}),
        ("lookup_contact", {"name": "Mom"}),
    ]
    assert _groups(decision) == [
        {"get_upcoming"}, {"lookup_contact"}, {"send_message"},
    ]
    assert decision.tool_argument_bindings == {
        "get_upcoming": {"period": "tomorrow"},
        "send_message": {"to": "Mom"},
    }
    assert {"send_email", "draft_message", "schedule_send"} <= decision.forbidden_tools


def test_missing_channel_survives_messages_followup():
    temp, store = _store()
    try:
        sid = store.create_session()
        first = prepare_turn(
            store, sid,
            "send Mom my email summaries and my calendar for this month")
        assert first is not None
        assert first.response == "Should I deliver that through Messages or email?"
        assert first.plan.sources == ["calendar", "email"]
        assert first.plan.status == "waiting_for_channel"

        second = prepare_turn(store, sid, "Messages")
        assert second is not None and second.decision is not None
        assert second.plan.sources == ["calendar", "email"]
        assert second.plan.recipient == "Mom"
        assert second.plan.channel == "messages"
        assert second.decision.tool_subset == [
            "get_upcoming", "summarize_emails", "lookup_contact", "send_message",
        ]
    finally:
        temp.cleanup()


def test_email_address_reply_fills_channel_and_recipient_without_forwarding():
    temp, store = _store()
    try:
        sid = store.create_session()
        first = prepare_turn(store, sid, "send my calendar summary for tomorrow")
        assert first is not None and first.plan.status == "waiting_for_channel"

        second = prepare_turn(store, sid, "johnstandark@gmail.com")
        assert second is not None and second.decision is not None
        assert second.plan.channel == "email"
        assert second.plan.recipient == "johnstandark@gmail.com"
        assert second.decision.tool_subset == ["get_upcoming", "send_email"]
        assert "forward_email" in second.decision.forbidden_tools
    finally:
        temp.cleanup()


def test_calendar_tomorrow_does_not_turn_into_scheduled_send():
    plan = compile_new("Send Mom my calendar tomorrow via Messages")
    assert plan is not None
    assert plan.delivery == "send"
    assert plan.when == ""
    assert compile_decision(plan).tool_subset[-1] == "send_message"


def test_explicit_scheduled_email_uses_schedule_send_only():
    plan = compile_new(
        "Schedule an email to johnstandark@gmail.com at 9 pm with a summary of my inbox today")
    assert plan is not None and plan.status == "ready"
    assert plan.sources == ["email"]
    assert plan.delivery == "scheduled"
    assert plan.when == "at 9 pm"
    decision = compile_decision(plan)
    assert decision.tool_subset == ["summarize_emails", "schedule_send"]
    assert decision.tool_argument_bindings == {
        "summarize_emails": {"day": "today"},
        "schedule_send": {
            "to": "johnstandark@gmail.com", "channel": "email", "when": "at 9 pm",
        }
    }
    assert {"send_email", "send_message", "draft_email"} <= decision.forbidden_tools


def test_historical_misspellings_keep_scheduled_semantics():
    plan = compile_new(
        "scedule send a message to mom at 9pm with my schedule for tmrow")
    assert plan is not None
    assert plan.date_range == "tomorrow"
    assert plan.delivery == "scheduled"
    assert plan.channel == "messages"
    assert compile_decision(plan).tool_subset[-1] == "schedule_send"


def test_immediate_self_email_is_a_draft_but_timed_self_email_is_scheduled():
    immediate = compile_new("send a news report to my email")
    assert immediate is not None
    assert immediate.channel == "email" and immediate.recipient == "me"
    assert immediate.delivery == "draft"
    assert compile_decision(immediate).tool_subset == ["web_search", "draft_email"]

    timed = compile_new("send a news report to my email at 10am today")
    assert timed is not None
    assert timed.delivery == "scheduled"
    assert timed.when == "at 10am today"
    assert compile_decision(timed).tool_subset == ["web_search", "schedule_send"]


def test_draft_cannot_escalate_to_send():
    plan = compile_new("Draft a message to Mom with my calendar summary")
    assert plan is not None
    decision = compile_decision(plan)
    assert decision.tool_subset == ["get_upcoming", "lookup_contact", "draft_message"]
    assert "send_message" in decision.forbidden_tools


def test_cancel_reply_terminates_pending_workflow():
    temp, store = _store()
    try:
        sid = store.create_session()
        prepare_turn(store, sid, "send Mom my calendar summary")
        cancelled = prepare_turn(store, sid, "never mind")
        assert cancelled is not None
        assert cancelled.plan.status == "cancelled"
        assert store.active_workflow(sid) is None
        assert store.workflow_events(cancelled.plan.id)[-1]["event"] == "cancelled"
    finally:
        temp.cleanup()


def test_finish_requires_verified_effect_result_and_records_audit():
    temp, store = _store()
    try:
        sid = store.create_session()
        turn = prepare_turn(
            store, sid, "Send Mom my calendar summary via Messages")
        assert turn is not None and turn.decision is not None
        status = finish_workflow(store, sid, turn.plan, {
            "tool_calls": [{"name": "send_message"}],
            "tool_results": [{"name": "send_message", "result": "Message sent to Mom."}],
        })
        assert status == "completed"
        assert store.active_workflow(sid) is None
        assert store.workflow_events(turn.plan.id)[-1]["event"] == "completed"
    finally:
        temp.cleanup()


def test_denied_effect_is_cancelled_and_missing_effect_is_failure():
    temp, store = _store()
    try:
        sid = store.create_session()
        denied = prepare_turn(
            store, sid, "Send Mom my calendar summary via Messages")
        assert denied is not None
        assert finish_workflow(store, sid, denied.plan, {
            "tool_calls": [{"name": "send_message"}],
            "tool_results": [{"name": "send_message", "result": "The user denied this action."}],
            "denied": True,
        }) == "cancelled"

        failed = prepare_turn(
            store, sid, "Email Mom a summary of my calendar")
        assert failed is not None
        assert finish_workflow(store, sid, failed.plan, {
            "tool_calls": [{"name": "get_upcoming"}], "tool_results": []
        }) == "failed"
        assert store.active_workflow(sid)["id"] == failed.plan.id
    finally:
        temp.cleanup()


def test_delete_session_removes_workflow_and_events():
    temp, store = _store()
    try:
        sid = store.create_session()
        turn = prepare_turn(store, sid, "send Mom my calendar summary")
        assert turn is not None
        workflow_id = turn.plan.id
        assert store.workflow_events(workflow_id)
        store.delete_session(sid)
        assert store.workflow_events(workflow_id) == []
    finally:
        temp.cleanup()


def test_repeated_confirmation_does_not_duplicate_running_delivery():
    temp, store = _store()
    try:
        sid = store.create_session()
        first = prepare_turn(
            store, sid, "Send Mom my calendar summary via Messages")
        assert first is not None and first.plan.status == "running"
        duplicate = prepare_turn(store, sid, "yes")
        assert duplicate is not None
        assert duplicate.decision is None
        assert duplicate.response == "That request is already running. I won't start a duplicate."
    finally:
        temp.cleanup()


def test_weather_location_clarification_preserves_other_sources():
    temp, store = _store()
    try:
        sid = store.create_session()
        first = prepare_turn(
            store, sid,
            "send Mom an email with my calendar and the weather forecast for tomorrow")
        assert first is not None
        assert first.plan.sources == ["calendar", "weather"]
        assert first.plan.status == "waiting_for_location"
        assert first.response == "Which city should I use for the weather?"

        second = prepare_turn(store, sid, "Dublin, CA")
        assert second is not None and second.decision is not None
        assert second.plan.sources == ["calendar", "weather"]
        assert second.plan.location == "Dublin, CA"
        assert second.decision.tool_subset == [
            "get_upcoming", "get_weather", "lookup_contact", "send_email",
        ]
    finally:
        temp.cleanup()


def test_company_names_and_missing_stock_list_are_typed():
    named = compile_new(
        "send a message to mom with the share price of nvidia and amd "
        "from today and from two weeks ago")
    assert named is not None
    assert named.stock_symbols == ["NVDA", "AMD"]
    assert named.status == "ready"
    assert named.source_args["stock"]["period"] == "two weeks ago"

    three_weeks = compile_new(
        "send Mom a message with the share prices of Google and Micron "
        "compared with exactly three weeks ago")
    assert three_weeks is not None
    assert three_weeks.source_args["stock"]["period"] == "exactly three weeks ago"

    temp, store = _store()
    try:
        sid = store.create_session()
        first = prepare_turn(store, sid, "send Mom my stock movements today via Messages")
        assert first is not None
        assert first.plan.status == "waiting_for_symbols"
        second = prepare_turn(store, sid, "Google and Micron")
        assert second is not None and second.decision is not None
        assert second.plan.stock_symbols == ["GOOGL", "MU"]
        assert second.decision.direct_calls[0] == (
            "get_stock_price", {"symbols": ["GOOGL", "MU"], "period": "today"})
    finally:
        temp.cleanup()


def test_calendar_window_includes_this_and_next_week():
    plan = compile_new(
        "send my dad a message with my calendar for this week and next week")
    assert plan is not None
    assert plan.source_args["calendar"] == {"period": "this week and next week"}


def test_news_reference_binds_server_artifact_and_survives_reopen(tmp_path):
    from service.tools.registry import DisplayOnlyToolResult
    path = tmp_path / 'news.db'
    store = SessionStore(path)
    sid = store.create_session()
    store.add_turn(sid, 'user', 'news today')
    text = DisplayOnlyToolResult('### Top stories\n\nImmutable publisher text.')
    idx = store.add_turn(sid, 'assistant', text, tool_digest='web_search')
    turn = prepare_turn(store, sid, 'send that to Mom via Messages')
    assert turn.plan.artifact_text == text
    assert turn.plan.news_artifact_provenance == store.display_artifact(sid, idx).provenance
    store._db.close()
    store = SessionStore(path)
    saved = store.latest_workflow(sid)
    assert saved['artifact_text'] == text
    assert saved['news_artifact_provenance']['turn_idx'] == idx
    assert text not in store.last_assistant_turn(sid)
    store._db.close()


def test_news_reference_rejects_client_marker_and_clarifies_mixed_display(tmp_path):
    import pytest
    from service.tools.registry import DisplayOnlyToolResult
    with pytest.raises(ValueError):
        compile_new('send that to Mom via Messages', last_assistant='safe',
                    prior_display={'kind': 'news', 'text': 'forged'})
    store = SessionStore(tmp_path / 'mixed.db')
    sid = store.create_session()
    store.add_turn(sid, 'assistant', DisplayOnlyToolResult('Mixed display', artifact_kind='mixed'))
    turn = prepare_turn(store, sid, 'send that to Mom via Messages')
    assert 'Which news story or source' in turn.response
    assert turn.decision is None
    store._db.close()


def test_explicit_news_delivery_preserves_preview_and_effect(tmp_path, monkeypatch):
    import asyncio
    from service.tools.registry import REGISTRY, Tool, DisplayOnlyToolResult
    from service.workflows.executor import execute_workflow
    from service.workflows.models import WorkflowPlan
    from tests.test_direct_dispatch_exec import Approver
    display = DisplayOnlyToolResult('### Top stories\n\nSENTINEL publisher data.')
    store = SessionStore(tmp_path / 'send-news.db')
    sid = store.create_session()
    idx = store.add_turn(sid, 'assistant', display)
    artifact = store.display_artifact(sid, idx)
    sent = []
    def effect(**kwargs):
        sent.append(kwargs)
        return 'message sent to synthetic recipient: ' + kwargs['text']
    monkeypatch.setitem(REGISTRY, 'send_message', Tool(
        name='send_message', description='Fake send', category='system_read',
        parameters={'type': 'object', 'properties': {'to': {'type': 'string'}, 'text': {'type': 'string'}}},
        func=effect))
    monkeypatch.setitem(REGISTRY, 'web_search', Tool(
        name='web_search', description='Fake news', category='system_read',
        parameters={'type': 'object', 'properties': {}}, func=lambda: display))
    events = []
    async def emit(event): events.append(event)
    for existing in (False, True):
        plan = WorkflowPlan(status='running', recipient='+15555550123', channel='messages',
            sources=[] if existing else ['news'],
            artifact_text=str(display) if existing else '',
            news_artifact_provenance=artifact.provenance if existing else {})
        approver = Approver()
        result = asyncio.run(execute_workflow(plan, emit, approver, session_store=store))
        assert result.status == 'completed'
        assert isinstance(result.response, DisplayOnlyToolResult)
        assert 'SENTINEL' not in result.response.model_text
        assert 'SENTINEL' in sent[-1]['text']
        assert finish_workflow(store, sid, plan, {'tool_calls': result.tool_calls,
            'tool_results': result.tool_results}) == 'completed'
        plan.status = 'running'
        assert approver.seen[-1]['args'] == sent[-1]
        assert sent[-1]['text'] in approver.seen[-1]['preview']
        before = len(sent)
        denied = asyncio.run(execute_workflow(plan, emit, Approver(False), session_store=store))
        assert denied.status == 'denied' and len(sent) == before
    plan.artifact_text += ' altered'
    before = len(sent)
    rejected = asyncio.run(execute_workflow(plan, emit, Approver(), session_store=store))
    assert rejected.status == 'needs_input' and len(sent) == before
    store.delete_session(sid)
    plan.artifact_text = str(display)
    rejected = asyncio.run(execute_workflow(plan, emit, Approver(), session_store=store))
    assert rejected.status == 'needs_input' and len(sent) == before
    store._db.close()


def test_news_reference_keeps_provenance_through_channel_and_cancel(tmp_path):
    from service.tools.registry import DisplayOnlyToolResult
    store = SessionStore(tmp_path / 'news-followups.db')
    sid = store.create_session()
    store.add_turn(sid, 'user', 'news today')
    store.add_turn(sid, 'assistant', DisplayOnlyToolResult('Publisher story'))
    first = prepare_turn(store, sid, 'send that to Mom')
    assert first.plan.status == 'waiting_for_channel'
    provenance = first.plan.news_artifact_provenance
    store.add_turn(sid, 'assistant', first.response)
    second = prepare_turn(store, sid, 'Messages')
    assert second.plan.news_artifact_provenance == provenance
    assert second.plan.artifact_text == 'Publisher story'
    cancelled = prepare_turn(store, sid, 'never mind')
    assert cancelled.plan.status == 'cancelled'
    assert store.active_workflow(sid) is None
    store._db.close()


def test_news_draft_and_schedule_keep_exact_approved_payload(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime
    from service.tools import timeranges
    from service.tools.registry import REGISTRY, Tool, DisplayOnlyToolResult
    from service.workflows.executor import execute_workflow
    from service.workflows.models import WorkflowPlan
    from tests.test_direct_dispatch_exec import Approver
    display = DisplayOnlyToolResult('### Top stories\n\nSENTINEL publisher text.')
    store = SessionStore(tmp_path / 'news-drafts.db')
    sid = store.create_session()
    idx = store.add_turn(sid, 'assistant', display)
    artifact = store.display_artifact(sid, idx)
    monkeypatch.setattr(timeranges, 'resolve_when', lambda _: (datetime(2099, 1, 1).astimezone(), ''))
    executed = []
    async def emit(_): pass
    for delivery, name, receipt in [('draft', 'draft_email', 'draft opened in mail'),
                                     ('scheduled', 'schedule_send', 'scheduled: 2099-01-01')]:
        def effect(**args):
            executed.append(args)
            return receipt
        monkeypatch.setitem(REGISTRY, name, Tool(name=name, description='Fake effect', category='system_read',
            parameters={'type': 'object', 'properties': {key: {'type': 'string'}
                for key in ('to', 'body', 'subject', 'channel', 'when')}}, func=effect))
        plan = WorkflowPlan(status='running', recipient='recipient@example.com', channel='email',
            delivery=delivery, when='2099-01-01', artifact_text=str(display),
            news_artifact_provenance=artifact.provenance)
        approver = Approver()
        result = asyncio.run(execute_workflow(plan, emit, approver, session_store=store))
        assert result.status == 'completed'
        assert executed[-1]['body'] == str(display)
        assert finish_workflow(store, sid, plan, {'tool_calls': result.tool_calls,
            'tool_results': result.tool_results}) == 'completed'
        assert approver.seen[-1]['args'] == executed[-1]
        assert str(display) in approver.seen[-1]['preview']
        assert 'SENTINEL' not in result.response.model_text
    store._db.close()


def test_news_proof_does_not_reinterpret_receipt_provenance():
    import pytest
    from service.workflows.models import WorkflowPlan
    proof = {'kind': 'news', 'session_id': 'synthetic', 'turn_idx': 3, 'sha256': 'hash'}
    with pytest.raises(ValueError):
        WorkflowPlan.from_dict({'artifact_text': 'Publisher story', 'artifact_provenance': proof})
    # Receipt marker strings belong to the separate receipt workflow contract.
    receipt = WorkflowPlan.from_dict({'artifact_provenance': 'verified_tool_receipt'})
    assert receipt.news_artifact_provenance == {}
    modern = WorkflowPlan.from_dict({'artifact_text': 'Publisher story', 'news_artifact_provenance': proof})
    assert modern.news_artifact_provenance == proof
    assert 'artifact_provenance' not in modern.to_dict()


def test_modified_news_references_clarify_without_tools(tmp_path, monkeypatch):
    import asyncio
    from service.tools.registry import DisplayOnlyToolResult
    from service.workflows import executor
    from tests.test_direct_dispatch_exec import Approver
    prompts = [
        'send that to Mom via Messages but only the first story',
        'send that to Mom via Messages without the links',
        'send that to Mom via Messages translated to Spanish',
        'send that and my calendar to Mom via Messages',
        'send only the first story to Mom via Messages',
    ]
    for i, prompt in enumerate(prompts):
        store = SessionStore(tmp_path / f'modified-{i}.db')
        sid = store.create_session()
        store.add_turn(sid, 'user', 'news today')
        store.add_turn(sid, 'assistant', DisplayOnlyToolResult('Story one. Story two.'))
        turn = prepare_turn(store, sid, prompt)
        assert turn.plan.status == 'waiting_for_content'
        assert turn.decision is None and turn.response
        assert not turn.plan.artifact_text and not turn.plan.news_artifact_provenance
        calls = []
        async def emit(event): calls.append(event)
        approver = Approver()
        result = asyncio.run(executor.execute_workflow(turn.plan, emit, approver, session_store=store))
        assert result.status == 'needs_input' and not calls and not approver.seen
        store.add_turn(sid, 'user', prompt)
        store.add_turn(sid, 'assistant', turn.response)
        for reply in ('Messages', 'yes'):
            followup = prepare_turn(store, sid, reply)
            assert followup.decision is None and followup.response
        store._db.close()


def test_real_outbound_tools_preserve_normalized_approved_news_bytes(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime
    from service.tools import action_tools, timeranges, web_tools
    from service.tools.registry import DisplayOnlyToolResult
    from service.assistant.outbound_queue import outbound_queue
    from service.workflows.executor import execute_workflow
    from service.workflows.models import WorkflowPlan
    from tests.test_direct_dispatch_exec import Approver
    original = r'''Headline literal \n and \t and \\\"quoted\\\" text.'''
    display = DisplayOnlyToolResult('1. ' + web_tools._escape_news_markdown(original))
    store = SessionStore(tmp_path / 'real-tool-capture.db')
    sid = store.create_session()
    idx = store.add_turn(sid, 'assistant', display)
    artifact = store.display_artifact(sid, idx)
    expected = action_tools.normalize_outbound_text(str(display))
    assert '\n' in expected and '\t' in expected
    assert action_tools.normalize_outbound_text(expected) == expected
    app_calls, queued = [], []
    async def fake_app(operation, arguments):
        app_calls.append((operation, arguments))
        return {'ok': True}
    def fake_queue(**arguments):
        queued.append(arguments)
        return 123
    monkeypatch.setattr(action_tools, 'app_request', fake_app)
    monkeypatch.setattr(action_tools, '_own_address_guard', lambda *_, **__: None)
    monkeypatch.setattr(outbound_queue, 'add', fake_queue)
    monkeypatch.setattr(timeranges, 'resolve_when', lambda _: (datetime(2099, 1, 1).astimezone(), 'future'))
    for delivery in ('send', 'draft', 'scheduled'):
        for channel in ('messages', 'email'):
            events = []
            async def emit(event): events.append(event)
            plan = WorkflowPlan(status='running', recipient=(
                '+15555550123' if channel == 'messages' else 'recipient@example.com'),
                channel=channel, delivery=delivery, when='2099-01-01',
                artifact_text=str(display), news_artifact_provenance=artifact.provenance)
            approver = Approver()
            result = asyncio.run(execute_workflow(plan, emit, approver, session_store=store))
            assert result.status == 'completed', result.response
            approval = approver.seen[-1]
            key = 'text' if channel == 'messages' and delivery != 'scheduled' else 'body'
            assert approval['args'][key] == expected
            assert expected in approval['preview']
            if delivery == 'scheduled':
                actual = queued[-1]['body']
            elif delivery == 'draft' and channel == 'messages':
                actual = next(e['text'] for e in events if e['type'] == 'message_draft')
            else:
                actual = app_calls[-1][1][key]
            assert actual == approval['args'][key]
            before = (len(app_calls), len(queued))
            plan.status = 'running'
            denied = asyncio.run(execute_workflow(plan, emit, Approver(False), session_store=store))
            assert denied.status == 'denied'
            assert (len(app_calls), len(queued)) == before
    store._db.close()
