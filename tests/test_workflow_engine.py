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
