"""Private-source constraints; routing only, no tools or models execute."""
import asyncio

import pytest

import service.tools
from service.router import router as R
from service.workflows.workflow20 import personal_read


def test_prohibition_optional_menu_cannot_activate_private_meeting(monkeypatch):
    async def core(_text):
        return ['plan_meeting', 'commit_private_meeting', 'clipboard_read']
    monkeypatch.setattr(R, '_semantic_core', core)
    decision = asyncio.run(R.route('Do not read my clipboard, it contains private information.'))
    assert not {'plan_meeting', 'commit_private_meeting'} & set(decision.tool_subset)
    assert not decision.direct_calls and not decision.required_tool_groups
    assert not decision.force_first_tool and not decision.expect_tool_first


def test_tomorrow_todo_actual_batch_reads_every_source_before_narration(monkeypatch):
    from service.agent import loop
    from service.workflows.reads import compile_read
    from tests.test_direct_dispatch_exec import Approver, ScriptedClient
    import socket
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError('No network, native process or unexpected source in this fixture')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr('service.memory.identity.identity_prompt_block', lambda **kwargs: '')
    prompt = "what's on my to-do list tomorrow?"
    decision = asyncio.run(R.route(prompt))
    assert compile_read(prompt) is None
    calls = []
    async def read(tool, args):
        calls.append((tool.name, args))
        return {'get_upcoming': 'Tomorrow appointment and dated native Reminder.',
                'summarize_messages': 'Earlier received: submit the blue form tomorrow.',
                'summarize_emails': 'Earlier received: bring the red folder tomorrow.',
                'search_notes': 'Undated: buy a green notebook; due date unknown.'}[tool.name]
    monkeypatch.setattr(loop, 'run_tool', read)
    class Client(ScriptedClient):
        async def stream_events(self, *args, **kwargs):
            assert calls == decision.direct_calls
            async for event in super().stream_events(*args, **kwargs):
                yield event
    client = Client([{'content': 'Synthetic narration after all source reads.'}])
    events = []
    async def emit(event): events.append(event)
    asyncio.run(loop.run_agent(client, 'Agents-A1-4B-oQe6',
        [{'role': 'user', 'content': decision.resolved_request}], emit, Approver(),
        tools=decision.tool_subset, direct_calls=decision.direct_calls,
        tool_argument_bindings=decision.tool_argument_bindings,
        narration_after=decision.narration_after, required_tool_groups=decision.required_tool_groups,
        strict_read_limits=decision.strict_read_limits, forbidden_tools=decision.forbidden_tools,
        multi_round=decision.multi_round,
        include_memory_context=False, max_steps=2))
    assert len(client.calls) == 1
    assert calls == decision.direct_calls
    assert not any(event['type'] == 'confirm' for event in events)


@pytest.mark.parametrize('prompt', [
    'plan arrival using traffic',
    'work backward from my arrival deadline',
    'get me there by tonight',
    'When should I leave home to reach SFO by 10:30 tonight?',
    'What time should I leave home to reach SFO by 10:30 tonight?',
    'How early should we leave home to reach SFO by 10:30 tonight?',
    'Plan me to go to SFO at 11PM with traffic and my location',
    'When do I need to leave to arrive at SFO by tomorrow at 11PM?',
    'When should I leave home to reach SFO by 10:30 tonight, then text Mom',
    'Plan my trip to SFO with traffic; then open Maps',
    'Plan arrival at SFO from home by 2026-10-12T23:00:00-07:00',
    'Plan arrival at SFO from here by 2026-10-12T23:00:00-07:00',
    'Plan arrival at SFO from current location by 2026-10-12T23:00:00-07:00',
    'Plan arrival at SFO from 123 Example Road, open Maps by 2026-10-12T23:00:00-07:00',
    'Plan arrival at SFO from 123 Example Road by 2026-10-12T23:00:00-07:00 with a 181-minute buffer',
])
def test_personal_arrival_clarifies_without_public_search_or_effect(prompt):
    assert R.rule_route(prompt) is None
    decision = asyncio.run(R.route(prompt))
    assert decision.direct_calls == []
    assert not decision.force_first_tool
    assert not decision.expect_tool_first
    assert not decision.tool_subset
    assert 'route_to_arrive' in decision.forbidden_tools
    assert {'web_search','web_fetch','http_request','run_shell','travel_time',
            'send_message','add_calendar_event'} <= decision.forbidden_tools
    assert decision.resolved_request.startswith(prompt)
    assert 'Clarify' in decision.resolved_request


def test_complete_arrival_has_only_exact_checked_read_binding():
    prompt = 'Plan arrival at SFO from 123 Example Road by 2026-10-12T23:00:00-07:00 with a 30-minute buffer'
    decision = asyncio.run(R.route(prompt))
    assert decision.tool_subset == ['route_to_arrive']
    assert decision.tool_argument_bindings == {'route_to_arrive': {
        'destination':'SFO', 'origin':'123 Example Road',
        'arrive_by':'2026-10-12T23:00:00-07:00',
        'timezone':'America/Los_Angeles', 'buffer_minutes':30}}
    assert not decision.direct_calls and not decision.force_first_tool
    assert 'travel_time' in decision.forbidden_tools


def test_unresolved_arrival_actual_loop_asks_once_without_tool_nudges(monkeypatch):
    import socket
    import subprocess
    from service.agent import loop
    from tests.test_direct_dispatch_exec import ScriptedClient,Approver
    def forbidden(*args,**kwargs):
        raise AssertionError('Clarification may not read providers or execute tools')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket.socket,'connect_ex',forbidden)
    monkeypatch.setattr(subprocess,'Popen',forbidden)
    monkeypatch.setattr(loop,'run_tool',forbidden)
    monkeypatch.setattr('service.memory.identity.identity_prompt_block',lambda **kwargs:'')
    prompt='When should I leave home to reach SFO by 10:30 tonight?'
    decision=asyncio.run(R.route(prompt))
    client=ScriptedClient([{'content':'What address should I use for home, and which arrival date?'}])
    events=[]
    async def emit(event): events.append(event)
    result=asyncio.run(loop.run_agent(client,'Agents-A1-4B-oQe6',
        [{'role':'user','content':decision.resolved_request}],emit,Approver(),
        tools=decision.tool_subset,expect_tool_first=decision.expect_tool_first,
        force_first_tool=decision.force_first_tool,required_tool_groups=decision.required_tool_groups,
        include_memory_context=False,max_steps=3))
    assert len(client.calls)==1
    assert 'address' in result and 'arrival date' in result
    assert not any(event['type'] in {'tool_call','confirm'} for event in events)


@pytest.mark.parametrize('prompt', [
    'Explain what the words "when should I leave to reach SFO" mean',
    'What is the history of SFO airport?',
    'What should tourists know about SFO traffic?',
])
def test_public_or_quoted_travel_topics_do_not_claim_personal_arrival(prompt):
    assert R._arrival_plan(prompt) is None


@pytest.mark.parametrize('prompt,args',[
    ('Give me today briefing.',{'period':'today'}),
    ('My daily digest, please.',{}),
    ('Give today briefing without weather.',{'period':'today','include_weather':False}),
    ('Give my daily briefing without email or texts.',{'include_email':False,'include_messages':False}),
])
def test_daily_envelope_variants_preserve_exclusions_at_both_entrypoints(prompt,args):
    from service.workflows.reads import compile_read
    from service.workflows.compiler import compile_new
    assert compile_new(prompt) is None
    assert compile_read(prompt)==([('daily_brief',args)],'')
    decision=asyncio.run(R.route(prompt))
    assert decision.direct_calls==[('daily_brief',args)]
    assert 'web_search' in decision.forbidden_tools


def test_calendar_only_brief_cannot_read_other_private_sources():
    from service.workflows.reads import compile_read
    prompt='Brief me on today calendar only.'
    assert compile_read(prompt)==([('get_upcoming',{'period':'today','calendar_only':True})],'')
    decision=asyncio.run(R.route(prompt))
    assert decision.tool_subset==['get_upcoming']


@pytest.mark.parametrize('prompt,args',[
    ('Recap the Studio crew group chat.',{'conversation':'Studio crew'}),
    ('Summarize the Family group from yesterday.',{'conversation':'Family','day':'yesterday'}),
    ('Catch me up on Studio crew chat from this week',{'conversation':'Studio crew','period':'this week'}),
])
def test_explicit_chat_recaps_bind_exact_conversation_and_range(prompt,args):
    from service.workflows.reads import compile_read
    from service.workflows.compiler import compile_new
    assert compile_new(prompt) is None
    assert compile_read(prompt)==([('summarize_messages',args)],'')
    decision=asyncio.run(R.route(prompt))
    assert decision.direct_calls==[('summarize_messages',args)]
    assert decision.tool_subset==['summarize_messages']
    assert 'web_search' in decision.forbidden_tools


@pytest.mark.parametrize('prompt',[
    'Summarize the Family group from yesterday and email Mom',
    'Recap the entire Studio crew chat',
    'Recap Studio crew group chat excluding what I wrote',
    'Recap Studio crew group chat about the budget',
    'Give today briefing without weather and open Maps',
    'Brief me on today calendar only and send it to Mom',
])
def test_new_read_grammars_never_drop_history_topic_sender_or_second_action(prompt):
    from service.workflows.workflow20 import daily_read,conversation_read
    assert daily_read(prompt) is None and conversation_read(prompt) is None


@pytest.mark.parametrize('prompt',[
    'What did Dad say yesterday?', 'What did Mom text today?',
    'What did my brother write yesterday?',
])
def test_relationship_communication_questions_remain_private_without_saved_contact(monkeypatch,prompt):
    monkeypatch.setattr('service.tools.imessage_tools.contact_names',lambda:[])
    decision=asyncio.run(R.route(prompt))
    assert 'web_search' in decision.forbidden_tools
    assert not any(name=='web_search' for name,args in decision.direct_calls)
    assert 'summarize_messages' in decision.tool_subset


def test_complete_excluded_source_names_do_not_count_as_required_reads():
    from service.workflows.reads import compile_read
    assert compile_read("What's on my calendar this week, do not include reminders")==([
        ('get_upcoming',{'period':'this week','calendar_only':True})],'')
    assert compile_read('show email summaries but not messages')==([('summarize_emails',{})],'')
    decision=asyncio.run(R.route("What's on my calendar this week, do not include reminders"))
    assert decision.direct_calls==[('get_upcoming',{'period':'this week','calendar_only':True})]
    assert decision.tool_subset==['get_upcoming']


@pytest.mark.parametrize('prompt',[
    'show email summaries but not messages about the budget',
    'show email summaries without private details',
    'show email summaries but not messages and email Mom',
    'show email summaries excluding what I wrote',
    "What's on my calendar this week, do not include reminders and open Maps",
])
def test_source_exclusion_shortcut_rejects_content_filters_and_additional_actions(prompt):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) is None


@pytest.mark.parametrize("prompt", [
    "What is happening across mail and texts today?",
    "Give an overview of tomorrow commitments and recent texts.",
    "Tell me about today email and yesterday messages.",
    "Give me an overview of my email, messages and calendar.",
    "Check my inbox and iMessages, then tell me what meetings are next.",
    "What should I know from my calender, email and mesages?",
    "Read my notes and reminders for groceries.",
])
def test_multisource_private_read_never_preexecutes_web(prompt):
    decision = asyncio.run(R.route(prompt))
    assert "web_search" in decision.forbidden_tools
    assert not any(name == "web_search" for name, _ in decision.direct_calls)
    assert len(decision.required_tool_groups) >= 2


@pytest.mark.parametrize("prompt,tool,day", [
    ("What unread texts do I have?", "view_messages", None),
    ("Catch me up on unread email.", "summarize_emails", None),
    ("Show my unread messages from yesterday", "view_messages", "yesterday"),
    ("List my unread texts today.", "view_messages", "today"),
    ("Read all unread email yesterday", "view_emails", "yesterday"),
])
def test_unread_binding_cannot_be_dropped(prompt, tool, day):
    decision = asyncio.run(R.route(prompt))
    assert decision.tool_argument_bindings[tool]["unread"] is True
    if day:
        assert decision.direct_calls == [(tool, {"unread": True, "day": day})]
    assert "web_search" in decision.forbidden_tools


def test_unread_cross_channel_complete_read():
    decision = asyncio.run(R.route("Show me all unread email and messages."))
    assert decision.direct_calls == [("view_emails", {"unread": True}), ("view_messages", {"unread": True})]
    assert decision.verified_results_only


@pytest.mark.parametrize("prompt", [
    "Show unread emails and messages I have read",
    "Show unread emails today and messages yesterday",
    "Show unread emails and messages from Mom yesterday",
    "Show unread email and messages that I opened",
])
def test_source_specific_unread_qualifiers_are_not_flattened(prompt):
    from service.workflows.reads import compile_read
    read = personal_read(prompt)
    assert read is None or (not read.direct_calls and not read.bindings)
    assert compile_read(prompt) is None


@pytest.mark.parametrize("prompt", [
    "Show unread emails I have read",
    "Show unread messages I opened",
    "What unread texts did I read?",
])
def test_unread_shortcut_requires_complete_grammar(prompt):
    read = personal_read(prompt)
    assert read is None or not read.direct_calls


@pytest.mark.parametrize("prompt", [
    'Show unread messages "urgent"', 'Show unread emails "from Mom"',
    'Show email summary from Priya', 'Show email summary and open Maps',
    'Show email summary containing "invoice"',
    'Show email summary and send it to Mom',
    'Show email and open Safari, purchases from Amazon',
])
def test_entrypoint_retains_named_quoted_and_second_action_qualifiers(prompt):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) is None


@pytest.mark.parametrize("prompt,args", [
    ("Show email summary", {}), ("Show my email summary from today", {"day": "today"}),
    ("Give me my inbox digest this week", {"period": "this week"}),
])
def test_complete_email_summary_shortcuts_remain_available(prompt, args):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) == ([("summarize_emails", args)], "")


def test_complete_purchase_lookup_remains_available():
    from service.workflows.reads import compile_read
    assert compile_read("Show email purchases from Amazon") == (
        [("view_emails", {"query": "Amazon", "strict_match": True})], "")
    assert compile_read("**Can you check my email for purchases from PlayStation?**") == (
        [("view_emails", {"query": "PlayStation", "strict_match": True})], "")


@pytest.mark.parametrize("prompt,status", [
    ("What texts and emails are scheduled?", "pending"),
    ("Show pending delivery IDs and times.", "pending"),
    ("Was the interrupted Lee email outcome confirmed?", "all"),
    ("Which sends have uncertain outcomes?", "all"),
    ("Which scheduled texts missed their time?", "all"),
    ("Is the interrupted Lee email safe to retry automatically?", "all"),
])
def test_scheduled_status_review_uses_recorded_history_not_pending_absence(prompt, status):
    decision = asyncio.run(R.route(prompt))
    assert decision.tool_argument_bindings["list_scheduled_sends"] == {"status": status}
    assert "web_search" in decision.forbidden_tools
    assert "send_message" in decision.forbidden_tools and "schedule_send" in decision.forbidden_tools
    assert "cancel_scheduled_send" not in decision.tool_subset


def test_scheduled_exact_id_inspection_and_cancel_require_existing_record():
    decision = asyncio.run(R.route("Inspect queued email ID abcdef123456 full body"))
    assert decision.tool_argument_bindings["list_scheduled_sends"] == {"id": "abcdef123456"}
    cancel = asyncio.run(R.route("Cancel pending email abcdef123456"))
    assert cancel.force_first_tool == "list_scheduled_sends"
    assert "cancel_scheduled_send" in cancel.tool_subset
    assert not cancel.direct_calls


def test_scheduled_explanation_does_not_read_queue():
    assert R._scheduled_send_review("Explain scheduled sends without cancelling") is None


@pytest.mark.parametrize("prompt", [
    "Show pending sends and my calendar", "Show queued texts and lock my screen",
    "Review pending emails then open Safari", "Show queued texts and delete files",
    "Review pending sends and cancel tomorrow's Mom text",
])
def test_queue_review_does_not_swallow_other_domains_or_effects(prompt):
    assert R._scheduled_send_review(prompt) is None


def test_quoted_queue_example_cannot_bind_record_identity():
    decision = asyncio.run(R.route('Show pending sends like "abcdef123456"'))
    assert decision.tool_argument_bindings["list_scheduled_sends"] == {"status": "pending"}


def test_queue_guidance_preserves_original_recipient_body_and_date():
    prompt = 'Show queued texts to Mom tomorrow containing "blue folder"'
    decision = asyncio.run(R.route(prompt))
    assert decision.resolved_request.startswith(prompt + "\n")


@pytest.mark.parametrize("prompt", [
    "Schedule a meeting with Priya tomorrow at 4PM",
    "Plan a 30 minute meeting with Lee after my test tomorrow",
    "Book a meeting with Mom after this event",
    "Schedule a meeting inviting Priya tomorrow at 4PM",
])
def test_meeting_proposal_preserves_request_without_substituting_invitation(prompt):
    from service.workflows.compiler import compile_new
    from service.workflows.reads import compile_read
    assert compile_new(prompt) is None
    assert compile_read(prompt) is None
    decision = asyncio.run(R.route(prompt))
    assert "plan_meeting" in decision.tool_subset
    assert "add_calendar_event" in decision.forbidden_tools
    assert not decision.direct_calls
    assert not decision.expect_tool_first
    assert decision.resolved_request.startswith(prompt + "\n")


def test_private_block_creation_offers_only_checked_approval_bound_effect():
    prompt = "Add a private calendar block for a 30 minute meeting after my exam tomorrow"
    decision = asyncio.run(R.route(prompt))
    assert "add_calendar_event" in decision.forbidden_tools
    assert "commit_private_meeting" in decision.tool_subset
    assert "commit_private_meeting" not in decision.forbidden_tools
    assert not decision.required_tool_groups
    assert not decision.direct_calls
    assert "send_message" in decision.forbidden_tools
    assert "web_search" in decision.forbidden_tools
    assert decision.resolved_request.startswith(prompt + "\n")
    assert "exact commit_args" in decision.resolved_request


@pytest.mark.parametrize("prompt", [
    "Add a private calendar block for a meeting tomorrow",
    'Create a private calendar block titled "Remind me tomorrow at 9am" for 30 minutes at 4PM tomorrow',
    "Create a private calendar block that invites Priya tomorrow at 4PM for 30 minutes",
])
def test_meeting_missing_inputs_and_quoted_titles_cannot_force_or_invent_effects(prompt):
    decision = asyncio.run(R.route(prompt))
    assert not decision.expect_tool_first
    assert not decision.required_tool_groups
    assert not decision.reminder_action
    assert "add_calendar_event" in decision.forbidden_tools
    assert "add_reminder" in decision.forbidden_tools


def test_calendar_exclusion_also_excludes_new_planner():
    decision = asyncio.run(R.route("Plan a meeting tomorrow, not Calendar"))
    assert "plan_meeting" in decision.forbidden_tools
    assert "plan_meeting" not in decision.tool_subset


@pytest.mark.parametrize("prompt", ["What should I work on now?", "What should I work on tomorrow?", "What do I need to do this week?"])
def test_priorities_can_recover_overdue_reminders(prompt):
    decision = asyncio.run(R.route(prompt))
    assert decision.tool_subset == ["collect_task_evidence"]
    assert "web_search" in decision.forbidden_tools
    period = "tomorrow" if "tomorrow" in prompt else "this week" if "this week" in prompt else "today"
    assert decision.tool_argument_bindings["collect_task_evidence"] == {
        "period": period, "include_overdue": True, "include_undated": True,
        **{f"include_{source}": True for source in ("email", "messages", "calendar", "notes", "reminders")}}
    assert decision.direct_calls == [("collect_task_evidence", decision.tool_argument_bindings["collect_task_evidence"])]
    assert decision.required_tool_groups == (frozenset({"collect_task_evidence"}),)
    assert "search_reminders" not in decision.tool_subset


@pytest.mark.parametrize("prompt,excluded", [
    ("What should I work on tomorrow without email and messages?", {"email", "messages"}),
    ("Could you help me prioritize my tasks this week using notes and reminders only", {"email", "messages", "calendar"}),
    ("What should I focus on tmrow from email", {"messages", "calendar", "notes", "reminders"}),
    ("What should I tackle this month excluding notes", {"notes"}),
])
def test_priority_envelope_preserves_exclusions_before_all_reads(prompt, excluded):
    from service.workflows.reads import compile_read
    decision = asyncio.run(R.route(prompt))
    assert compile_read(prompt) is None
    args = decision.tool_argument_bindings["collect_task_evidence"]
    assert {name.removeprefix("include_") for name, value in args.items() if name.startswith("include_")
            and name not in {"include_overdue", "include_undated"} and value is False} == excluded
    assert "web_search" in decision.forbidden_tools
    assert decision.narration_after == frozenset({"collect_task_evidence"})


@pytest.mark.parametrize("prompt", [
    "What should I work on tomorrow and email Maya", "What should I work on using web",
    "What should I work on if Maya agrees", "What should I work on tomorrow at 5pm",
    "Explain what should I work on", "What should I buy for groceries and place an order",
    "What should I get for groceries using email", "What should I get for groceries without checking notes",
])
def test_evidence_grammar_does_not_drop_additional_conditions_or_actions(prompt):
    from service.workflows.workflow20 import priority_read
    assert priority_read(prompt) is None


@pytest.mark.parametrize("prompt", ["What should I work on tomorrow without email"])
def test_evidence_collectors_execute_before_first_model_narration(monkeypatch, prompt):
    from service.agent import loop
    from tests.test_direct_dispatch_exec import Approver, ScriptedClient
    import socket
    import subprocess
    def forbidden(*args, **kwargs): raise AssertionError("no network/native effect")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("service.memory.identity.identity_prompt_block", lambda **kwargs: "")
    decision = asyncio.run(R.route(prompt))
    calls = []
    async def read(tool, args):
        calls.append((tool.name, args))
        return "Synthetic source-backed evidence; no action occurred."
    monkeypatch.setattr(loop, "run_tool", read)
    class Client(ScriptedClient):
        async def stream_events(self, *args, **kwargs):
            assert calls == decision.direct_calls
            async for event in super().stream_events(*args, **kwargs): yield event
    client = Client([{"content": "Synthetic suggestion with source uncertainty."}])
    events = []
    async def emit(event): events.append(event)
    asyncio.run(loop.run_agent(client, "Agents-A1-4B-oQe6", [{"role": "user", "content": decision.resolved_request}],
        emit, Approver(), tools=decision.tool_subset, direct_calls=decision.direct_calls,
        tool_argument_bindings=decision.tool_argument_bindings, narration_after=decision.narration_after,
        required_tool_groups=decision.required_tool_groups, forbidden_tools=decision.forbidden_tools,
        multi_round=decision.multi_round, include_memory_context=False, max_steps=2))
    assert calls == decision.direct_calls and len(client.calls) == 1
    assert not any(event["type"] == "confirm" for event in events)


@pytest.mark.parametrize("prompt,args", [
    ("Daily summary", {}),
    ("Give me a daily summary for tomorrow", {"period": "tomorrow"}),
    ("My daily brief without email or messages", {"include_email": False, "include_messages": False}),
    ("Daily digest next week, no calendar and weather", {"period": "next week", "include_calendar": False, "include_weather": False}),
])
def test_daily_envelope_preserves_date_and_source_constraints(prompt, args):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) == ([("daily_brief", args)], "")
    decision = asyncio.run(R.route(prompt))
    assert decision.direct_calls == [("daily_brief", args)]
    assert decision.tool_argument_bindings["daily_brief"] == args
    assert "web_search" in decision.forbidden_tools


@pytest.mark.parametrize("prompt", [
    "Daily summary only work emails", "Daily summary without reading anything",
    "Daily summary then send it to Mom", "Daily summary if Mom agrees",
    "Daily summary but no medical details", "Daily summary with weather in Santa Clara",
])
def test_daily_shortcut_does_not_drop_unconsumed_constraints(prompt):
    from service.workflows.workflow20 import daily_read
    assert daily_read(prompt) is None


def test_agenda_preserves_both_dst_fold_offsets(monkeypatch):
    import datetime as dt
    import time
    from zoneinfo import ZoneInfo
    from service.tools.assistant_tools import _agenda_item
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        rows = []
        for fold in (0, 1):
            when = dt.datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/Los_Angeles"), fold=fold)
            rows.append(_agenda_item({"title": "Synthetic event", "when_ts": when.timestamp(), "source": "calendar"}))
        assert "1:30 AM PDT-0700" in rows[0]
        assert "1:30 AM PST-0800" in rows[1]
        assert rows[0] != rows[1]
    finally:
        monkeypatch.undo()
        time.tzset()


def test_missing_meeting_duration_can_ask_through_real_loop_without_tools(monkeypatch):
    from service.agent import loop
    from service.memory import identity
    prompt = "Add a private calendar block for a meeting tomorrow"
    decision = asyncio.run(R.route(prompt))
    question = "What duration and start time should I use for the private block?"
    choices = []
    def forbidden(*args, **kwargs):
        raise AssertionError("No reader, approval or effect may execute in this clarification control")
    monkeypatch.setattr(loop, "run_tool", forbidden)
    monkeypatch.setattr(identity, "identity_prompt_block", lambda **kwargs: "")
    class Client:
        managed = True
        base_url = "http://127.0.0.1:8000"
        async def ensure_only(self, *args, **kwargs):
            pass
        async def stream_events(self, model, messages, **kwargs):
            choices.append(kwargs.get("tool_choice"))
            assert any(prompt in str(row.get("content", "")) for row in messages)
            yield {"kind": "final", "message": {"role": "assistant", "content": question}}
    class Approver:
        confirm = forbidden
    async def emit(event):
        pass
    result = asyncio.run(loop.run_agent(Client(), decision.model,
        [{"role": "user", "content": decision.resolved_request}], emit, Approver(),
        tools=decision.tool_subset, required_tool_groups=decision.required_tool_groups,
        forbidden_tools=decision.forbidden_tools, expect_tool_first=decision.expect_tool_first,
        include_memory_context=False, max_steps=1, multi_round=decision.multi_round))
    assert choices == ["auto"]
    assert result == question


@pytest.mark.parametrize("prompt", [
    "Plan a meeting after restarting Chrome",
    "Plan a meeting after checking traffic",
    "Add a private calendar block for a 30 minute meeting tomorrow at 4PM after Priya agrees",
    "Plan a meeting tomorrow. Lock my screen",
    "Plan a meeting tomorrow using current news from the web",
    "Plan a meeting tomorrow based on my email",
    "Plan a meeting tomorrow while opening Safari",
    "Schedule a meeting then text Mom",
    "Plan a meeting and check battery",
    "Schedule a private block without checking calendar",
    "Add a private calendar block if Priya agrees",
    'Explain "schedule a meeting tomorrow"',
])
def test_meeting_shortcut_does_not_swallow_constraints_or_other_actions(prompt):
    assert R._meeting_plan(prompt) is None


@pytest.mark.parametrize("group_by,name,child", [
    ("extension", "protected.csv", "CSV"),
    ("debug_date", "debug-2026-10-10.log", "2026-10-10"),
])
def test_grouped_organization_policy_checks_actual_child_target(tmp_path, monkeypatch, group_by, name, child):
    from service.safety import policy
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / name).write_text("synthetic")
    args = {"pattern": "*", "folder": str(folder), "destination": ".", "group_by": group_by}
    operands = dict(policy._derived_path_operands("organize_files", args))
    assert operands["source child"] == str(folder / name)
    assert operands["destination child"] == str(folder / child / name)
    import re
    prior = policy._rules
    monkeypatch.setattr(policy, "_rules", lambda key, default: [re.escape(str(folder / child))] if key == "path_deny" else prior(key, default))
    assert policy._hard_deny("fs_write", args, "organize_files").tier == policy.Tier.DENY


def test_organization_policy_excludes_symlinks_and_unclassified_dates(tmp_path):
    from service.safety import policy
    (tmp_path / "data.txt").write_text("synthetic")
    (tmp_path / "link.log").symlink_to(tmp_path / "data.txt")
    args = {"pattern": "*", "folder": str(tmp_path), "destination": ".", "group_by": "debug_date"}
    operands = list(policy._derived_path_operands("organize_files", args))
    assert set(operands) == {("source child", str(tmp_path / "data.txt")),
                             ("source child", str(tmp_path / "link.log"))}
    assert not any(key == "destination child" for key, _ in operands)


@pytest.mark.parametrize("group_by,name", [
    ("extension", "link.log"), ("debug_date", "debug-2026-10-10.log"),
    ("debug_date", "unclassified.log"),
])
def test_grouped_symlink_source_retains_protected_target_floor(tmp_path, monkeypatch, group_by, name):
    import re
    from service.safety import policy
    source = tmp_path / "source"
    source.mkdir()
    protected = tmp_path / "protected.log"
    protected.write_text("synthetic")
    link = source / name
    link.symlink_to(protected)
    args = {"pattern": "*", "folder": str(source), "destination": ".", "group_by": group_by}
    assert list(policy._derived_path_operands("organize_files", args)) == [("source child", str(link))]
    prior = policy._rules
    monkeypatch.setattr(policy, "_rules", lambda key, default: [re.escape(str(protected))] if key == "path_deny" else prior(key, default))
    assert policy._hard_deny("fs_write", args, "organize_files").tier == policy.Tier.DENY


def test_grouped_destination_symlink_retains_protected_target_floor(tmp_path, monkeypatch):
    import re
    from service.safety import policy
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.csv").write_text("synthetic")
    protected = tmp_path / "protected"
    protected.mkdir()
    (source / "CSV").symlink_to(protected, target_is_directory=True)
    args = {"pattern": "*", "folder": str(source), "destination": ".", "group_by": "extension"}
    prior = policy._rules
    monkeypatch.setattr(policy, "_rules", lambda key, default: [re.escape(str(protected))] if key == "path_deny" else prior(key, default))
    assert policy._hard_deny("fs_write", args, "organize_files").tier == policy.Tier.DENY


def test_route_diagnostic_freezes_synthetic_clock_and_restores_environment():
    import datetime
    import os
    import time
    from scripts.run_workflow20_benchmark import scenario_context
    from service.tools.imessage_tools import contact_names
    prior = os.environ.get("TZ")
    real_datetime = datetime.datetime
    scenario = {"clock": "2026-10-12T10:00:00-07:00", "timezone": "America/Los_Angeles",
                "sources": {"contacts": {"state": "ready", "records": [{"name": "Synthetic Mom"}]}}}
    with scenario_context(scenario):
        from datetime import datetime as late_import
        from service.tools.imessage_tools import contact_names as seeded_names
        assert late_import.now().isoformat() == "2026-10-12T10:00:00"
        assert datetime.date.today().isoformat() == "2026-10-12"
        assert time.time() == real_datetime.fromisoformat(scenario["clock"]).timestamp()
        assert seeded_names() == ["Synthetic Mom"]
    assert os.environ.get("TZ") == prior
    assert datetime.datetime is real_datetime


@pytest.mark.parametrize("prompt", [
    "Send Mom my unread email summary", "Draft Maya my messages and calendar",
    "Delete unread email and messages", "Mark unread messages read",
    "Email Maya my calendar", "Schedule a meeting from my messages",
    "Compare Apple Mail and Calendar features", "Search the web for email and calendar apps",
    "Read my email without messages", "What is email?",
    "What did my Mom say in messages?",
    "Read my email and tell Mom my calendar", "Read my messages and put this on my calendar",
    "Read my calendar and email Mom the summary", "Read my email and text Mom the summary",
    "Read my messages and schedule a meeting", "Show unread email and all messages today",
    "How do email and calendar work?",
    "Show my unread email and lock my screen", "Show my unread email and open Safari",
    "Summarize my notes and calendar then play music",
    "Show unread email. Open Safari", "Show unread email while opening Safari",
    "Read my email! Lock my screen", "Read my calendar after opening Maps",
    "Show unread messages about email", "Show unread messages about my calendar",
    "Show unread messages related to email", "Show unread messages mentioning email",
    "Show unread email and show my battery level",
    "Read my notes and reminders and check disk space", "Show unread email, open Safari",
])
def test_nonmatching_or_complex_requests_keep_existing_interpretation(prompt):
    assert personal_read(prompt) is None


@pytest.mark.parametrize("prompt", [
    'Show my messages containing "email and calendar"',
    'Read the note titled "email and calendar"',
])
def test_quoted_words_are_not_extra_personal_sources(prompt):
    from service.workflows.workflow20 import mentioned_sources
    assert "email" not in mentioned_sources(prompt) and "calendar" not in mentioned_sources(prompt)


@pytest.mark.parametrize("prompt", ["Show unread work email summary", "Show unread email summary from Priya"])
def test_qualified_unread_shortcut_does_not_drop_qualifiers(prompt):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) is None


def test_named_unread_does_not_guess_conversation_or_drop_topic():
    decision = asyncio.run(R.route("Show unread messages from Mom about Saturday."))
    assert decision.direct_calls == []
    assert decision.tool_argument_bindings["view_messages"] == {"unread": True}
    assert decision.required_tool_groups == (frozenset({"view_messages", "summarize_messages"}),)


@pytest.mark.parametrize("prompt", [
    "Give me an email summary and my messages", "Give me my email summary and calendar",
    "Read my mail and texts today", "Read email and draft Mom my calendar",
    "Summarize only email without messages", 'Explain the phrase "unread email and messages"',
])
def test_actual_read_entrypoint_never_swallows_compound_request(prompt):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) is None


def test_actual_read_entrypoint_shares_complete_unread_plan():
    from service.workflows.reads import compile_read
    calls, response = compile_read("Show unread email and messages from yesterday")
    assert calls == [("view_emails", {"unread": True, "day": "yesterday"}),
                     ("view_messages", {"unread": True, "day": "yesterday"})]
    assert response == ""


@pytest.mark.parametrize("prompt", [
    "Calendar a 90-minute Friday workshop at 4PM in Building A",
    "Please calendar a 30 minute appointment tomorrow at 2PM",
    "Could you calendar a study block tomorrow at 3PM for 45 minutes",
])
def test_calendar_as_verb_retains_confirmed_creation_route(prompt):
    decision = asyncio.run(R.route(prompt))
    assert "add_calendar_event" in decision.tool_subset
    assert "add_calendar_event" not in decision.forbidden_tools
    assert not decision.direct_calls
    assert not decision.reminder_action


@pytest.mark.parametrize("prompt", [
    "What is on my calendar tomorrow?",
    "Calendar appointments tomorrow",
    "Show my calendar and workshop",
    'Explain "calendar a workshop tomorrow"',
])
def test_calendar_as_verb_does_not_turn_reads_into_writes(prompt):
    assert not R._CALENDAR_WRITE_RE.search(R.mask_quoted(prompt))


@pytest.mark.parametrize("prompt", [
    "Cancel Maya agenda email", "Cancel Maya agenda email but leave Lee weekly email",
    "Please cancel the Mom summary text",
])
def test_named_delivery_cancel_requires_read_only_discovery_and_new_exact_selection(prompt):
    decision = asyncio.run(R.route(prompt))
    assert "list_scheduled_sends" in decision.tool_subset
    assert "cancel_scheduled_send" not in decision.tool_subset
    assert "cancel_scheduled_send" in decision.forbidden_tools
    assert "unconditional single exact-ID" in decision.resolved_request
    assert decision.tool_argument_bindings["list_scheduled_sends"] == {"status": "pending"}
    assert not decision.direct_calls
    assert decision.resolved_request.startswith(prompt + "\n")
    assert {"send_message", "send_email", "schedule_send"} <= decision.forbidden_tools


@pytest.mark.parametrize("prompt,args", [
    ("What is the weather in San Francisco, California today?", {"location":"San Francisco, California","period":"today"}),
    ("Weather in Santa Clara, CA tomorrow", {"location":"Santa Clara, CA","period":"tomorrow"}),
    ("Check weather for Santa Cruz, California", {"location":"Santa Cruz, California","period":""}),
])
def test_city_weather_uses_verified_provider_without_general_web(prompt,args):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) == ([("get_weather",args)],"")
    decision = asyncio.run(R.route(prompt))
    assert decision.direct_calls == [("get_weather",args)]
    assert not any(name=="web_search" for name,_ in decision.direct_calls)


@pytest.mark.parametrize("prompt,args", [
    ("What time is my Budget review this week?", {"calendar_only":True,"query":"Budget review","period":"this week"}),
    ("When does our physics quiz two start?", {"calendar_only":True,"query":"physics quiz two","days":60}),
    ("When is my phsyics midterm?", {"calendar_only":True,"query":"phsyics midterm","days":60}),
    ("When is my dentist appointment on my calendar?", {"calendar_only":True,"query":"dentist appointment","days":60}),
])
def test_named_event_question_preserves_title_identity_and_bounded_horizon(prompt,args):
    from service.workflows.reads import compile_read
    assert compile_read(prompt) == ([("get_upcoming",args)],"")
    decision = asyncio.run(R.route(prompt))
    assert decision.direct_calls == [("get_upcoming",args)]
    assert "web_search" in decision.forbidden_tools


def test_which_unread_arrived_today_keeps_both_filters():
    decision = asyncio.run(R.route("Which unread emails arrived today?"))
    assert decision.direct_calls == [("view_emails",{"unread":True,"day":"today"})]


@pytest.mark.parametrize("prompt", [
    "What is the weather in Santa Clara and text Mom?",
    "Search the web for weather in Santa Clara",
    "What is the weather in SF without the web?",
    "When is my test from my work account?", "When is my test next semester?",
    "What time is my dentist appointment and lock my screen?",
])
def test_new_typed_read_grammars_decline_unconsumed_constraints(prompt):
    from service.workflows.workflow20 import weather_read,calendar_question
    assert weather_read(prompt) is None and calendar_question(prompt) is None


@pytest.mark.parametrize('suffix', ['without reminders', 'skip reminders',
    'excluding reminders', 'leave out reminders', 'do not include reminders'])
def test_calendar_exclusion_synonyms_remain_exact_verified_reads(suffix):
    prompt = f"Show my calendar this week {suffix}"
    decision = asyncio.run(R.route(prompt))
    assert decision.direct_calls == [('get_upcoming', {'period':'this week','calendar_only':True})]
    assert decision.verified_results_only
    assert decision.tool_subset == ['get_upcoming']


def test_generic_calendar_keeps_established_scope_and_context_isolation():
    from service import main
    prompt = "what is on my calendar"
    decision = asyncio.run(R.route(prompt))
    assert decision.direct_calls == [('get_upcoming', {'days':60})]
    assert main._current_schedule_source_route(decision, prompt)
    assert not decision.narration_after


@pytest.mark.parametrize('day, expected', [
    ('2026-10-09', '2026-10-10..2026-10-11'),
    ('2026-10-10', '2026-10-10..2026-10-11'),
    ('2026-10-11', '2026-10-10..2026-10-11'),
    ('2026-10-12', '2026-10-17..2026-10-18'),
    ('2026-10-31', '2026-10-31..2026-11-01'),
    ('2026-12-31', '2027-01-02..2027-01-03'),
])
def test_weekend_calendar_alias_has_executable_exact_private_range(monkeypatch, day, expected):
    from datetime import date, datetime
    from service.workflows import reads
    from service.tools import timeranges
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(day + 'T12:00:00')
    monkeypatch.setattr(reads, 'datetime', Clock)
    prompt = 'is there anything on my cal this weekend'
    args = reads._calendar_read_args(prompt, '', date.fromisoformat(day))
    assert args == {'period': expected}
    from datetime import timedelta
    start, end, _ = timeranges.resolve_span(expected)
    first, last = expected.split('..')
    assert start == datetime.fromisoformat(first).timestamp()
    assert end == (datetime.fromisoformat(last) + timedelta(days=1)).timestamp()
    decision = asyncio.run(R.route(prompt))
    assert decision.direct_calls == [('get_upcoming', args)]
    assert decision.tool_subset == ['get_upcoming']
    assert decision.verified_results_only
    assert {'web_search', 'web_fetch', 'http_request'} <= decision.forbidden_tools


@pytest.mark.parametrize('prompt', [
    'is there anything on my cal this weekend and text Mom',
    'is there anything on my cal this weekend about physics',
    'is there anything on my cal next weekend',
    'is there anything on my cal this weekend then open Maps',
])
def test_weekend_calendar_parser_declines_unrepresented_constraints(prompt):
    from service.workflows.reads import _calendar_read_args
    assert _calendar_read_args(prompt, '') is None


def test_public_weather_does_not_use_personal_data_voice():
    decision = asyncio.run(R.route('What is the weather in Tokyo?'))
    assert decision.direct_calls == [('get_weather', {'location':'Tokyo','period':''})]
    assert decision.tool_subset == ['get_weather']
    assert not decision.light_read


@pytest.fixture
def exact_cancel_queue(tmp_path, monkeypatch):
    from service.assistant import outbound_queue as queues
    from service.assistant.outbound_queue import OutboundQueue
    from service.tools import action_tools
    from service.safety import policy, grants
    from service.agent import loop
    import socket
    import subprocess
    queue = OutboundQueue(tmp_path / 'fictional-cancel.sqlite')
    monkeypatch.setattr(queues, 'outbound_queue', queue)
    def forbidden(*args, **kwargs): raise AssertionError('no native/network/model effects')
    async def async_forbidden(*args, **kwargs): forbidden()
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(action_tools, 'app_request', async_forbidden)
    from service.assistant import outbox
    monkeypatch.setattr(outbox, 'request', async_forbidden)
    monkeypatch.setattr(policy, '_FULL_ACCESS', False)
    monkeypatch.setattr(policy, '_READ_ONLY', False)
    monkeypatch.setattr(grants, 'check', lambda *args, **kwargs: None)
    monkeypatch.setattr(loop, 'audit', lambda *args, **kwargs: None)
    monkeypatch.setattr('service.memory.identity.identity_prompt_block', lambda **kwargs: '')
    yield queue
    queue._db.close()


def _exact_cancel_loop(monkeypatch, queue, *, read_error=False, missing=False, read_only=False,
                       claim_after_read=False, duplicate=False, suffix='', quoted_id=False, malformed_id=False):
    from service.agent import loop
    from service.safety import policy
    from tests.test_direct_dispatch_exec import ScriptedClient, Approver, _tool_call
    a = queue.add(channel='email', recipient='a@synthetic.invalid', body='Fixture A', when_ts=2000000000)
    b = queue.add(channel='email', recipient='b@synthetic.invalid', body='Fixture B', when_ts=2000000000)
    target = '000000000000' if missing else a
    selector = target + '0' if malformed_id else target
    prompt = f'Cancel pending email ' + (f'"{selector}"' if quoted_id else selector) + suffix
    decision = asyncio.run(R.route(prompt))
    monkeypatch.setattr(policy, '_READ_ONLY', read_only)
    actual = loop.run_tool
    calls = []
    async def run(tool, args):
        calls.append((tool.name, dict(args)))
        if tool.name == 'list_scheduled_sends' and read_error:
            return '(error: synthetic queue read unavailable)'
        result = await actual(tool, args)
        if tool.name == 'list_scheduled_sends' and claim_after_read:
            assert queue.claim(a)
        return result
    monkeypatch.setattr(loop, 'run_tool', run)
    script = [{'tool_calls': [_tool_call('list_scheduled_sends', {'id': b}, 'read')]},
              {'tool_calls': [_tool_call('cancel_scheduled_send', {'id': b}, 'cancel')]}]
    if duplicate:
        script.append({'tool_calls': [_tool_call('cancel_scheduled_send', {'id': b}, 'duplicate')]})
    script.append({'content': 'Synthetic result; actual state is asserted separately.'})
    client, approver, events = ScriptedClient(script), Approver(), []
    async def emit(event): events.append(event)
    asyncio.run(loop.run_agent(client, 'Agents-A1-4B-oQe6', [{'role':'user','content':prompt}],
        emit, approver, tools=decision.tool_subset, force_first_tool=decision.force_first_tool,
        expect_tool_first=decision.expect_tool_first, required_tool_groups=decision.required_tool_groups,
        tool_argument_bindings=decision.tool_argument_bindings, forbidden_tools=decision.forbidden_tools,
        multi_round=decision.multi_round, include_memory_context=False, max_steps=8))
    return a, b, target, calls, events


def test_exact_cancel_actual_loop_cannot_switch_to_another_pending_id(monkeypatch, exact_cancel_queue):
    queue = exact_cancel_queue
    a, b, _, calls, events = _exact_cancel_loop(monkeypatch, queue, duplicate=True)
    states = {key: queue.get(key)['status'] for key in (a, b)}
    assert states == {a:'cancelled', b:'pending'}, (states, calls)
    assert calls == [('list_scheduled_sends', {'id': a}), ('cancel_scheduled_send', {'id': a})]
    assert any(event['type'] == 'tool_result' and event['result'].startswith('Cancelled [' + a) for event in events)


@pytest.mark.parametrize('prompt', [
    'Cancel pending email abcdef123456', 'Please stop my queued text ID abcdef123456.',
    'Cancel the scheduled message with ID abcdef123456!', 'Stop queued delivery abcdef123456',
])
def test_exact_cancel_complete_clause_binds_same_literal_to_both_tools(prompt):
    decision = asyncio.run(R.route(prompt))
    assert decision.tool_argument_bindings['list_scheduled_sends'] == {'id':'abcdef123456'}
    assert decision.tool_argument_bindings['cancel_scheduled_send'] == {'id':'abcdef123456'}
    assert decision.required_tool_groups == (frozenset({'list_scheduled_sends'}),)
    assert decision.force_first_tool == 'list_scheduled_sends' and not decision.direct_calls


@pytest.mark.parametrize('prompt', [
    'Cancel pending email abcdef123456 if Maya agrees',
    'Cancel pending email abcdef123456 unless it is urgent',
    'Cancel pending email abcdef123456 and abcdef123457',
    'Cancel pending email abcdef123456 but keep abcdef123456',
    'Cancel pending email abcdef123456 containing "if Maya agrees"',
    'Cancel pending email abcdef123456 tomorrow',
    'Cancel pending email abcdef123456 "if Maya agrees"',
    'Cancel pending email abcdef1234567 if Maya agrees',
    'Cancel pending email abcdef12345 unless it is urgent',
    'Cancel pending email',
    'Cancel pending ID abcdefabcde email',
    'Cancel Maya email "if Maya agrees"',
    'Cancel Maya email "abcdef1234567"',
    'Cancel Maya email but keep it if Maya objects',
])
def test_exact_cancel_unconsumed_conditions_never_offer_cancellation(prompt):
    decision = asyncio.run(R.route(prompt))
    assert 'cancel_scheduled_send' not in decision.tool_subset
    assert 'cancel_scheduled_send' in decision.forbidden_tools
    assert 'cancel_scheduled_send' not in decision.tool_argument_bindings


@pytest.mark.parametrize('mode', ['read_error','missing','read_only','claim_after_read'])
def test_exact_cancel_actual_loop_negative_outcomes_never_touch_other_id(monkeypatch, exact_cancel_queue, mode):
    queue = exact_cancel_queue
    a, b, target, calls, events = _exact_cancel_loop(monkeypatch, queue, **{mode:True})
    assert queue.get(b)['status'] == 'pending'
    assert queue.get(a)['status'] == ('sending' if mode == 'claim_after_read' else 'pending')
    assert not any(event['type']=='tool_result' and event['result'].startswith('Cancelled [') for event in events)
    assert all(args['id'] == target for _, args in calls)
    if mode in {'read_error','read_only'}:
        assert all(name != 'cancel_scheduled_send' for name, _ in calls)


@pytest.mark.parametrize('suffix', [' if Maya agrees', ' unless it is urgent', ' "if Maya agrees"'])
def test_exact_cancel_actual_loop_conditional_request_cannot_mutate(monkeypatch, exact_cancel_queue, suffix):
    queue = exact_cancel_queue
    a, b, _, calls, events = _exact_cancel_loop(monkeypatch, queue, suffix=suffix)
    assert queue.get(a)['status'] == queue.get(b)['status'] == 'pending'
    assert all(name != 'cancel_scheduled_send' for name, _ in calls)
    assert not any(event['type']=='tool_result' and event['result'].startswith('Cancelled [') for event in events)


@pytest.mark.parametrize('suffix', ['', ' if Maya agrees'])
def test_exact_cancel_quoted_id_never_grants_unbound_effect(monkeypatch, exact_cancel_queue, suffix):
    queue = exact_cancel_queue
    a, b, _, calls, _ = _exact_cancel_loop(monkeypatch, queue, quoted_id=True, suffix=suffix)
    assert queue.get(a)['status'] == queue.get(b)['status'] == 'pending', calls
    assert all(name != 'cancel_scheduled_send' for name, _ in calls)


def test_exact_cancel_malformed_id_never_grants_unbound_effect(monkeypatch, exact_cancel_queue):
    queue = exact_cancel_queue
    a, b, _, calls, _ = _exact_cancel_loop(monkeypatch, queue, malformed_id=True, suffix=' if Maya agrees')
    assert queue.get(a)['status'] == queue.get(b)['status'] == 'pending', calls
    assert all(name != 'cancel_scheduled_send' for name, _ in calls)


@pytest.mark.parametrize('prompt', ['Cancel Maya agenda email',
    'Cancel Maya agenda email but leave Lee weekly email', 'Please cancel the Mom summary text'])
def test_named_cancel_actual_loop_requires_new_user_exact_id(monkeypatch, exact_cancel_queue, prompt):
    from service.agent import loop
    from tests.test_direct_dispatch_exec import ScriptedClient, Approver, _tool_call
    queue = exact_cancel_queue
    a = queue.add(channel='email', recipient='maya@synthetic.invalid', display='Maya', subject='Agenda',
                  body='Fictional Maya agenda', when_ts=2000000000)
    b = queue.add(channel='email', recipient='lee@synthetic.invalid', display='Lee', subject='Weekly',
                  body='Fictional Lee weekly', when_ts=2000000000)
    calls = []
    actual = loop.run_tool
    async def run(tool, args):
        calls.append((tool.name, dict(args)))
        return await actual(tool, args)
    monkeypatch.setattr(loop, 'run_tool', run)
    async def turn(request, script):
        decision = await R.route(request)
        events = []
        async def emit(event): events.append(event)
        client, approver = ScriptedClient(script), Approver()
        await loop.run_agent(client, 'Agents-A1-4B-oQe6', [{'role':'user','content':decision.resolved_request}],
            emit, approver, tools=decision.tool_subset, force_first_tool=decision.force_first_tool,
            expect_tool_first=decision.expect_tool_first, required_tool_groups=decision.required_tool_groups,
            tool_argument_bindings=decision.tool_argument_bindings, forbidden_tools=decision.forbidden_tools,
            multi_round=decision.multi_round, include_memory_context=False, allow_skill_context=False, max_steps=8)
        return events, approver
    events, approver = asyncio.run(turn(prompt, [
        {'tool_calls':[_tool_call('list_scheduled_sends', {'status':'pending'}, 'read')]},
        {'tool_calls':[_tool_call('cancel_scheduled_send', {'id':b}, 'hostile')]},
        {'content':'Please choose the exact queue ID.'}]))
    assert {key:queue.get(key)['status'] for key in (a,b)} == {a:'pending', b:'pending'}, calls
    assert calls == [('list_scheduled_sends', {'status':'pending'})]
    result = next(e['result'] for e in events if e['type']=='tool_result' and e['id']=='read')
    assert a in result and b in result
    assert approver.seen == []
    # A separate, unconditional human request mints the exact target binding.
    calls.clear()
    asyncio.run(turn('Cancel scheduled message ' + a, [
        {'tool_calls':[_tool_call('list_scheduled_sends', {'id':b}, 'read-selected')]},
        {'tool_calls':[_tool_call('cancel_scheduled_send', {'id':b}, 'cancel-selected')]},
        {'content':'Synthetic exact-target result.'}]))
    assert calls == [('list_scheduled_sends', {'id':a}), ('cancel_scheduled_send', {'id':a})]
    assert {key:queue.get(key)['status'] for key in (a,b)} == {a:'cancelled', b:'pending'}


@pytest.mark.parametrize('response', ['yes', 'go ahead'])
def test_named_cancel_discovery_acknowledgement_never_authorizes_cancellation(response):
    d = asyncio.run(R.route(response, last_user='Cancel Maya agenda email',
        last_assistant='Choose the exact queue ID before I can cancel it.', last_tools='list_scheduled_sends'))
    assert 'cancel_scheduled_send' not in (d.tool_subset or ())
    assert 'cancel_scheduled_send' in d.forbidden_tools


# Real HTTP entry controls reuse disposable stores and blocked native/model sinks.
from tests.test_workflow20_knowledge import knowledge_entrypoint, storage


def _http_queue_pair(queue):
    a = queue.add(channel='email', recipient='maya@synthetic.invalid', display='Maya',
                  body='Fictional agenda', when_ts=2000000000)
    b = queue.add(channel='email', recipient='lee@synthetic.invalid', display='Lee',
                  body='Fictional weekly update', when_ts=2000000000)
    return a, b


def _http_queue_script(read_args, cancel_id=None, duplicate=False):
    from tests.test_direct_dispatch_exec import _tool_call
    script = [{'tool_calls':[_tool_call('list_scheduled_sends', read_args, 'read')]}]
    if cancel_id:
        script.append({'tool_calls':[_tool_call('cancel_scheduled_send', {'id':cancel_id}, 'cancel')]})
    if duplicate:
        script.append({'tool_calls':[_tool_call('cancel_scheduled_send', {'id':cancel_id}, 'duplicate')]})
    return script + [{'content':'Synthetic queue evidence; choose an exact ID when needed.'}]


@pytest.mark.parametrize('honest', [False, True])
@pytest.mark.parametrize('duplicate', [False, True])
def test_queue_actual_http_exact_id_reaches_checked_scope(knowledge_entrypoint, exact_cancel_queue, honest, duplicate):
    state, queue = knowledge_entrypoint, exact_cancel_queue
    a, b = _http_queue_pair(queue)
    selected = a if honest else b
    events = state.run('Cancel pending email ' + a, _http_queue_script({'id':selected}, selected, duplicate), transport='asgi')
    assert queue.get(b)['status'] == 'pending'
    if honest:
        assert queue.get(a)['status'] == 'cancelled'
    else:
        assert queue.get(a)['status'] in {'pending', 'cancelled'}
    assert state.tools == [('list_scheduled_sends', {'id':a}), ('cancel_scheduled_send', {'id':a})]
    assert state.early == state.native == state.skill_selection == state.skill_context == []
    assert not any(e['type'] in {'task_plan','workflow','confirm','error'} for e in events), events


@pytest.mark.parametrize('template', [
    'Cancel Maya agenda email', 'Cancel Maya agenda email but leave Lee weekly email',
    'Cancel pending email "{a}"', 'Cancel pending email {a} if Maya agrees',
    'Cancel pending email {a} and {b}', 'Cancel pending email {a}0',
    'Show pending email {a}', 'Cancel pending email {a} unless Lee objects'])
def test_queue_actual_http_unbound_targets_are_read_only(knowledge_entrypoint, exact_cancel_queue, template):
    state, queue = knowledge_entrypoint, exact_cancel_queue
    a, b = _http_queue_pair(queue)
    prompt = template.format(a=a,b=b)
    events = state.run(prompt, _http_queue_script({'status':'pending'}, b), transport='asgi')
    assert {key:queue.get(key)['status'] for key in (a,b)} == {a:'pending',b:'pending'}, state.tools
    assert [name for name,_ in state.tools] == ['list_scheduled_sends']
    assert state.early == state.native == state.skill_selection == state.skill_context == []
    assert not any(e['type'] in {'task_plan','workflow','confirm','error'} for e in events), events


@pytest.mark.parametrize('failure', ['read', 'claim'])
def test_queue_actual_http_read_failure_or_claim_blocks_cancel(knowledge_entrypoint, exact_cancel_queue, monkeypatch, failure):
    state, queue = knowledge_entrypoint, exact_cancel_queue
    from service.agent import loop
    a, b = _http_queue_pair(queue)
    actual = loop.run_tool
    async def controlled(tool,args):
        if tool.name == 'list_scheduled_sends' and failure == 'read':
            state.tools.append((tool.name,dict(args)))
            return '(error: fictional queue unavailable)'
        result = await actual(tool,args)
        if tool.name == 'list_scheduled_sends' and failure == 'claim':
            assert queue.claim(a)
        return result
    monkeypatch.setattr(loop, 'run_tool', controlled)
    events = state.run('Cancel pending email ' + a, _http_queue_script({'id':a},a), transport='asgi')
    assert queue.get(a)['status'] == ('sending' if failure == 'claim' else 'pending')
    assert queue.get(b)['status'] == 'pending'
    if failure == 'read':
        assert all(name != 'cancel_scheduled_send' for name,_ in state.tools)
    assert state.early == state.native == []
    assert not any(e['type'] in {'task_plan','workflow','confirm','error'} for e in events), events


@pytest.mark.parametrize('named', [False, True])
def test_queue_http_preserves_unrelated_pending_plans_and_skill(knowledge_entrypoint, exact_cancel_queue, monkeypatch, named):
    from pathlib import Path
    from service import skills
    from service.tasks.models import TaskPlan, SlotValue
    from service.workflows.models import WorkflowPlan
    state, queue = knowledge_entrypoint, exact_cancel_queue
    a,b = _http_queue_pair(queue)
    sid = state.sessions.create_session()
    state.sessions.save_workflow(sid, TaskPlan(intent='reminder.create', status='waiting_for_input',revision=4,
        subject=SlotValue('Unrelated fictional task')).to_dict())
    state.sessions.save_workflow(sid, WorkflowPlan(sources=['email'],status='waiting_for_channel',revision=7,
        original_request='Unrelated fictional summary').to_dict())
    state.sessions.set_active_skill(sid,'queue-fixture')
    marker='SUSPENDED QUEUE SKILL MARKER'
    monkeypatch.setattr(skills, '_skills', {'queue-fixture':skills.Skill('queue-fixture','synthetic',marker,
        Path('/synthetic/not-read'),conversation_workflow=True)})
    before=[tuple(row) for row in state.sessions._db.execute('SELECT id,status,state_json,updated_at FROM workflows ORDER BY id')]
    prompt='Cancel Maya agenda email' if named else 'Cancel pending email ' + a
    events=state.run(prompt,_http_queue_script({'status':'pending'} if named else {'id':a},b),sid=sid,transport='asgi')
    assert before == [tuple(row) for row in state.sessions._db.execute('SELECT id,status,state_json,updated_at FROM workflows ORDER BY id')]
    assert state.sessions.get_session(sid)['active_skill']=='queue-fixture'
    import json
    assert marker not in json.dumps(state.clients[0].calls)
    assert state.early == state.native == state.skill_selection == state.skill_context == []
    assert queue.get(b)['status']=='pending'
    assert queue.get(a)['status']==('pending' if named else 'cancelled')
    assert not any(e['type'] in {'task_plan','workflow','confirm','error'} for e in events), events


@pytest.mark.parametrize('reply', ['yes','go ahead'])
def test_queue_http_bare_acknowledgement_does_not_select_cancellation(knowledge_entrypoint, exact_cancel_queue, reply):
    state,queue=knowledge_entrypoint,exact_cancel_queue
    a,b=_http_queue_pair(queue)
    events=state.run('Cancel Maya agenda email',_http_queue_script({'status':'pending'}),transport='asgi')
    sid=events[0]['id']
    state.tools.clear()
    state.run(reply,_http_queue_script({'id':b},b),sid=sid,transport='asgi')
    assert state.tools==[]
    assert {key:queue.get(key)['status'] for key in (a,b)} == {a:'pending',b:'pending'}
    assert state.native==[]


def test_queue_http_view_only_keeps_every_pending_record(knowledge_entrypoint, exact_cancel_queue, monkeypatch):
    from service.safety import policy
    state, queue = knowledge_entrypoint, exact_cancel_queue
    a,b = _http_queue_pair(queue)
    monkeypatch.setattr(policy, '_READ_ONLY', True)
    events=state.run('Cancel pending email ' + a,_http_queue_script({'id':a},a),transport='asgi')
    assert {key:queue.get(key)['status'] for key in (a,b)} == {a:'pending',b:'pending'}
    assert all(name != 'cancel_scheduled_send' for name,_ in state.tools)
    assert state.early == state.native == []
    assert not any(e['type'] in {'task_plan','workflow','confirm','error'} for e in events)


@pytest.mark.parametrize('prompt', ['Remind me to buy milk tomorrow at 6PM',
    'Draft an email to maya@synthetic.invalid saying Hello'])
def test_queue_http_guard_preserves_genuine_mail_and_reminder_interpreters(knowledge_entrypoint, exact_cancel_queue, prompt):
    state,queue=knowledge_entrypoint,exact_cancel_queue
    a,b=_http_queue_pair(queue)
    assert R._scheduled_send_review(prompt) is None
    state.run(prompt,[{'content':'Synthetic ordinary task response.'}],approve=False,transport='asgi')
    assert 'task' in state.early
    assert all(name not in {'list_scheduled_sends','cancel_scheduled_send'} for name,_ in state.tools)
    assert {key:queue.get(key)['status'] for key in (a,b)} == {a:'pending',b:'pending'}
    assert state.native==[]


@pytest.mark.parametrize('prompt', ["What's queued to send", 'What is queued to send?',
    'Show me queued to send', 'List queued to send', 'Show what is queued to send',
    'Which emails are queued to send?', 'Please check currently queued to send'])
def test_canonical_queue_read_actual_http_cannot_mutate_or_narrow_scope(knowledge_entrypoint, exact_cancel_queue, prompt):
    from tests.test_direct_dispatch_exec import _tool_call
    state,queue=knowledge_entrypoint,exact_cancel_queue
    a,b=_http_queue_pair(queue)
    script=_http_queue_script({'status':'history','id':b,'count':1},b)
    script.insert(-1,{'tool_calls':[_tool_call('schedule_send', {'channel':'email','to':'new@synthetic.invalid',
        'text':'Unexpected fictional send','when':'tomorrow at 6PM'},'schedule')]})
    events=state.run(prompt,script,transport='asgi')
    assert state.tools==[('list_scheduled_sends', {'status':'pending','id':'','count':20})]
    assert {key:queue.get(key)['status'] for key in (a,b)}=={a:'pending',b:'pending'}
    assert {row["id"] for row in queue.review("pending",100)[0]} == {a,b}
    result=next(e['result'] for e in events if e['type']=='tool_result' and e['id']=='read')
    assert a in result and b in result
    assert state.early==state.native==state.skill_selection==state.skill_context==[]
    assert not any(e['type'] in {'task_plan','workflow','confirm','error'} for e in events)


@pytest.mark.parametrize('suffix', [' and remind me to buy milk', ' if Maya agrees',
    ' then send Lee a message', ' without emails', ' tomorrow only'])
def test_canonical_queue_read_never_swallows_extra_constraints(suffix):
    assert R._scheduled_send_review("What's queued to send"+suffix) is None


@pytest.mark.parametrize('verb', ['Cancel', 'Stop'])
@pytest.mark.parametrize('hostile', [False, True])
def test_queued_send_literal_id_actual_http_targets_only_current_id(knowledge_entrypoint, exact_cancel_queue, verb, hostile):
    state,queue=knowledge_entrypoint,exact_cancel_queue
    a,b=_http_queue_pair(queue)
    prompt=f'{verb} queued send {a}'
    decision=asyncio.run(R.route(prompt))
    assert decision.tool_argument_bindings.get('cancel_scheduled_send')=={'id':a}
    events=state.run(prompt,_http_queue_script({'id':a},b if hostile else a),transport='asgi')
    assert state.tools==[('list_scheduled_sends',{'id':a}),('cancel_scheduled_send',{'id':a})]
    assert {key:queue.get(key)['status'] for key in (a,b)}=={a:'cancelled',b:'pending'}
    assert state.early==state.native==[]
    assert not any(e['type'] in {'task_plan','workflow','confirm','error'} for e in events)


def _injected_queue_decision(target='abcdef123456', *, broad=False):
    decision=R.RouteDecision('agent','synthetic',True,'rules','preserve reason',
        route_source='preserve source',expect_tool_first=True,
        tool_subset=None if broad else ['list_scheduled_sends','cancel_scheduled_send','get_battery_status'],
        force_first_tool='list_scheduled_sends',multi_round=True,
        required_tool_groups=(frozenset({'list_scheduled_sends'}),),
        tool_argument_bindings={'list_scheduled_sends':{'id':target},'cancel_scheduled_send':{'id':target}},
        resolved_request='Cancel pending email '+target)
    return decision


def test_public_route_body_signature_and_single_await_are_preserved():
    import ast,hashlib,inspect,textwrap
    implementation=ast.parse(textwrap.dedent(inspect.getsource(R._route_impl))).body[0]
    wrapper=ast.parse(textwrap.dedent(inspect.getsource(R.route))).body[0]
    body=ast.dump(ast.Module(body=implementation.body,type_ignores=[]),include_attributes=False)
    signature=ast.dump(wrapper.args,include_attributes=False)
    assert hashlib.sha256(body.encode()).hexdigest()=='f4c3e7684b0777be41a0a1a2392247c6eb50e5205a2ea49bd760547764dbc05f'
    assert hashlib.sha256(signature.encode()).hexdigest()=='edcb3e9f9d06ef15a39eb04dadef72be9995af5c66f86b8f0bb19290f1e00c02'
    assert inspect.signature(R.route)==inspect.signature(R._route_impl)
    assert len([n for n in ast.walk(wrapper) if isinstance(n,ast.Await)])==1


@pytest.mark.parametrize('prompt', ["What's queued to send",'yes','go ahead','Cancel Maya agenda email',
    'Cancel pending email "abcdef123456"','Cancel pending email abcdef123456 if Maya agrees',
    'Cancel pending email abcdef123456 and show my notes','Cancel pending email abcdef123456 and 123456abcdef',
    'Explain "Cancel pending email abcdef123456"','Cancel pending email abcdef1234560'])
def test_public_queue_guard_never_authorizes_from_history_or_resolved_metadata(monkeypatch,prompt):
    decision=_injected_queue_decision()
    calls=[]
    async def implementation(text,**kwargs):
        calls.append((text,kwargs));return decision
    monkeypatch.setattr(R,'_route_impl',implementation)
    history={'last_user':'Cancel pending email abcdef123456','recent_users':['Cancel pending email abcdef123456'],
        'last_assistant':'Cancel pending email abcdef123456','last_tools':'successful read abcdef123456'}
    result=asyncio.run(R.route(prompt,**history))
    if prompt == 'Explain "Cancel pending email abcdef123456"':
        # A completely consumed quoted explanation now owns the request
        # before generic routing. Even action-shaped history grants no tools.
        assert not calls
        assert not result.needs_tools and result.tool_subset == []
        assert not result.direct_calls and not result.tool_argument_bindings
        assert not result.required_tool_groups and not result.force_first_tool
        assert 'cancel_scheduled_send' in result.forbidden_tools
        return
    assert result is decision
    assert calls==[(prompt,history)]
    assert 'cancel_scheduled_send' in result.forbidden_tools
    assert 'cancel_scheduled_send' not in result.tool_subset
    assert 'cancel_scheduled_send' not in result.tool_argument_bindings
    assert result.resolved_request=='Cancel pending email abcdef123456'


@pytest.mark.parametrize('damage', ['wrong_read','wrong_cancel','extra_read_arg','extra_cancel_arg',
    'missing_read','missing_cancel','missing_group','mixed_group','missing_force','read_forbidden',
    'read_not_offered','direct_cancel'])
def test_public_queue_guard_requires_existing_same_target_and_successful_read_contract(monkeypatch,damage):
    d=_injected_queue_decision()
    if damage=='wrong_read':d.tool_argument_bindings['list_scheduled_sends']={'id':'123456abcdef'}
    elif damage=='wrong_cancel':d.tool_argument_bindings['cancel_scheduled_send']={'id':'123456abcdef'}
    elif damage=='extra_read_arg':d.tool_argument_bindings['list_scheduled_sends']['status']='all'
    elif damage=='extra_cancel_arg':d.tool_argument_bindings['cancel_scheduled_send']['other']='value'
    elif damage=='missing_read':d.tool_argument_bindings.pop('list_scheduled_sends')
    elif damage=='missing_cancel':d.tool_argument_bindings.pop('cancel_scheduled_send')
    elif damage=='missing_group':d.required_tool_groups=()
    elif damage=='mixed_group':d.required_tool_groups=(frozenset({'list_scheduled_sends','get_battery_status'}),)
    elif damage=='missing_force':d.force_first_tool=None
    elif damage=='read_forbidden':d.forbidden_tools=frozenset({'list_scheduled_sends'})
    elif damage=='read_not_offered':d.tool_subset=['cancel_scheduled_send']
    elif damage=='direct_cancel':d.direct_calls=[('cancel_scheduled_send',{'id':'abcdef123456'})]
    async def implementation(*args,**kwargs):return d
    monkeypatch.setattr(R,'_route_impl',implementation)
    result=asyncio.run(R.route('Cancel pending email abcdef123456'))
    assert 'cancel_scheduled_send' in result.forbidden_tools
    assert 'cancel_scheduled_send' not in result.tool_argument_bindings
    assert all(name!='cancel_scheduled_send' for name,_ in result.direct_calls)


@pytest.mark.parametrize('broad',[False,True])
def test_public_queue_guard_removes_every_cancel_carrier_and_preserves_unrelated_metadata(monkeypatch,broad):
    import copy
    cancel='cancel_scheduled_send'; d=_injected_queue_decision(broad=broad)
    d.force_first_tool=cancel
    d.direct_calls=[(cancel,{'id':'abcdef123456'}),('get_battery_status',{})]
    d.required_tool_groups=(frozenset({cancel}),frozenset({cancel,'get_battery_status'}),frozenset({'list_scheduled_sends'}))
    d.forbidden_tools=frozenset({'run_shell'})
    d.conditional_tools=(('list_scheduled_sends',cancel,'contains',('pending',)),
        (cancel,'get_battery_status','contains',('pending',)),('get_battery_status','toggle_setting','percent_below',20))
    d.narration_after=frozenset({cancel,'get_battery_status'})
    d.strict_read_limits={cancel:1,'get_battery_status':2}
    expected=copy.deepcopy(d)
    expected.forbidden_tools|=frozenset({cancel})
    if not broad:expected.tool_subset.remove(cancel)
    expected.force_first_tool=None
    expected.direct_calls=[('get_battery_status',{})]
    expected.required_tool_groups=(frozenset(),frozenset({'get_battery_status'}),frozenset({'list_scheduled_sends'}))
    expected.tool_argument_bindings.pop(cancel)
    expected.conditional_tools=(('get_battery_status','toggle_setting','percent_below',20),)
    expected.narration_after=frozenset({'get_battery_status'})
    expected.strict_read_limits={'get_battery_status':2}
    async def implementation(*args,**kwargs):return d
    monkeypatch.setattr(R,'_route_impl',implementation)
    assert asyncio.run(R.route('Show my pending messages'))==expected


@pytest.mark.parametrize('existing_denial',[False,True])
def test_public_queue_guard_preserves_exact_id_contract_and_existing_denial(monkeypatch,existing_denial):
    import copy
    d=_injected_queue_decision()
    if existing_denial:d.forbidden_tools=frozenset({'cancel_scheduled_send'})
    before=copy.deepcopy(d)
    async def implementation(*args,**kwargs):return d
    monkeypatch.setattr(R,'_route_impl',implementation)
    assert asyncio.run(R.route('Cancel pending email abcdef123456'))==before


@pytest.mark.parametrize('failure',[RuntimeError('synthetic implementation error'),asyncio.CancelledError()])
def test_public_queue_guard_propagates_implementation_errors_and_cancellation(monkeypatch,failure):
    calls=[]
    async def implementation(*args,**kwargs):calls.append((args,kwargs));raise failure
    monkeypatch.setattr(R,'_route_impl',implementation)
    with pytest.raises(type(failure)) as caught:asyncio.run(R.route('Show my pending messages'))
    assert caught.value is failure
    assert len(calls)==1


def test_public_queue_guard_has_no_cross_turn_authority(monkeypatch):
    async def implementation(text,**kwargs):
        await asyncio.sleep(0)
        return _injected_queue_decision('123456abcdef' if '123456abcdef' in text else 'abcdef123456')
    monkeypatch.setattr(R,'_route_impl',implementation)
    async def run():
        return await asyncio.gather(R.route('Cancel pending email abcdef123456'),
            R.route('Show pending messages',last_user='Cancel pending email abcdef123456'),
            R.route('Cancel pending email 123456abcdef'))
    a,read,b=asyncio.run(run())
    assert a.tool_argument_bindings['cancel_scheduled_send']=={'id':'abcdef123456'}
    assert b.tool_argument_bindings['cancel_scheduled_send']=={'id':'123456abcdef'}
    assert 'cancel_scheduled_send' in read.forbidden_tools
    assert 'cancel_scheduled_send' not in read.tool_argument_bindings


@pytest.mark.parametrize('prompt', ['Show my calendar today','Show my grocery list',
    'What should I work on tomorrow?','When is my test?','What did Maya say?',
    'Never browse the web','Read Wisp knowledge titled "Synthetic"','What is my battery level?',
    'List my pending messages','What is queued to send?'])
def test_public_route_all_return_paths_restrict_unrequested_cancel(prompt,exact_cancel_queue,monkeypatch):
    async def core(*args,**kwargs):return ['list_scheduled_sends','cancel_scheduled_send']
    monkeypatch.setattr(R,'_semantic_core',core)
    d=asyncio.run(R.route(prompt))
    assert 'cancel_scheduled_send' in d.forbidden_tools
    assert 'cancel_scheduled_send' not in (d.tool_subset or ())
    assert 'cancel_scheduled_send' not in d.tool_argument_bindings


def test_public_queue_guard_actual_generic_read_loop_cannot_cancel(monkeypatch,exact_cancel_queue):
    from service.agent import loop
    from tests.test_direct_dispatch_exec import Approver,ScriptedClient
    queue=exact_cancel_queue;a,b=_http_queue_pair(queue)
    async def implementation(*args,**kwargs):return _injected_queue_decision(b,broad=True)
    monkeypatch.setattr(R,'_route_impl',implementation)
    prompt='Show recorded queue information'
    d=asyncio.run(R.route(prompt,last_user='Cancel pending email '+b))
    events=[]
    async def emit(event):events.append(event)
    client=ScriptedClient(_http_queue_script({'id':b},b))
    asyncio.run(loop.run_agent(client,'Agents-A1-4B-oQe6',[{'role':'user','content':prompt}],emit,Approver(),
        tools=d.tool_subset,force_first_tool=d.force_first_tool,expect_tool_first=d.expect_tool_first,
        required_tool_groups=d.required_tool_groups,tool_argument_bindings=d.tool_argument_bindings,
        forbidden_tools=d.forbidden_tools,multi_round=d.multi_round,include_memory_context=False,max_steps=6))
    assert {key:queue.get(key)['status'] for key in (a,b)}=={a:'pending',b:'pending'}
    assert not any(e.get('name')=='cancel_scheduled_send' and e['type']=='tool_start' for e in events)
