"""Fast paths may act only on a request they fully understand.

Independent audit of #129/#131 reproduced these against the complete candidates:
  * negated / quoted device text ("Do not lock my screen.") direct-dispatched a call;
  * a quoted sentence ('Say "remind me to call Mom tomorrow".') became a real reminder;
  * calendar shortcuts dropped a second source, a named date and an exclusion;
  * an inbox shortcut flattened modifiers into the sender query;
  * authored prose ("Email Sam asking to move our meeting") forced a Calendar read.
Each case below is asserted at the layer that owned the bug, with controls proving
the supported plain requests still take their fast path.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import json
from dataclasses import replace
from datetime import date, datetime

import pytest

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-fastpath-"))

from service.skills import load as load_skills  # noqa: E402

load_skills()

import service.router.router as R  # noqa: E402
from service import utterance_shape as U  # noqa: E402
from service.tasks.compiler import compile_task  # noqa: E402
from service.workflows import reads  # noqa: E402
from service.workflows.compiler import compile_new  # noqa: E402

NOW = datetime(2026, 9, 30, 10, 0)


def route(text):
    return asyncio.run(R.route(text))


# --- the shared definition ---------------------------------------------------------------

PROHIBITIONS = ["Do not lock my screen.", "don't lock my screen", "Please do not email Sam.",
                "Never delete that file", "No need to send it", "Hey Wisp, don't text Mom"]
MENTIONS = ['Explain the phrase "lock my screen".', 'Read the word "clipboard" aloud.',
            'Say "remind me to call Mom tomorrow".', 'What does "lock my screen" mean?',
            'Translate "send an email to Sam"', 'Define the word clipboard', 'Repeat "delete everything"']
PLAIN = ["lock my screen", "what's on my clipboard", "remind me to call Mom tomorrow at 5pm",
         "Don't forget to call Mom tomorrow", "Don't let me forget the dentist", "Never mind, I did it",
         "Stop the music", "email Sam saying \"running late\"", 'Say "hi" to Mom', "text Mom \"I'm on my way\"",
         "what's on my calendar, don't include reminders", "turn on do not disturb",
         "remind me not to forget my keys", "Don't worry about it"]


@pytest.mark.parametrize("text", PROHIBITIONS)
def test_a_prohibition_is_not_an_instruction(text):
    assert U.deliberate(text), text


@pytest.mark.parametrize("text", MENTIONS)
def test_a_sentence_about_words_is_not_an_instruction(text):
    assert U.deliberate(text), text


@pytest.mark.parametrize("text", PLAIN)
def test_plain_requests_are_left_alone(text):
    assert U.deliberate(text) is None, text


def test_quoted_words_are_masked_for_retrieval():
    assert U.mask_quoted('Say "remind me to call Mom" now') == 'Say "…" now'
    assert U.mask_quoted("it's Sam's, don't") == "it's Sam's, don't"


# --- P1: direct device dispatch ----------------------------------------------------------

@pytest.mark.parametrize("text", ["Do not lock my screen.", 'Explain the phrase "lock my screen".',
                                  'Read the word "clipboard" aloud.', "Don't read my clipboard."])
def test_negated_or_quoted_device_text_makes_no_call(text):
    decision = route(text)
    assert decision.direct_calls == [], decision.reason
    assert decision.force_first_tool is None and not decision.expect_tool_first, decision.reason


@pytest.mark.parametrize("text,tool", [("lock my screen", "lock_screen"),
                                       ("what's on my clipboard", "clipboard_read"),
                                       ("what's my battery level", "get_battery_status")])
def test_a_plain_device_request_still_takes_the_direct_path(text, tool):
    assert [n for n, _ in route(text).direct_calls] == [tool]


# --- P1: a quoted request must not become a task ----------------------------------------

def test_a_quoted_reminder_is_not_a_reminder():
    text = 'Say "remind me to call Mom tomorrow".'
    assert compile_task(text, now=NOW) is None
    assert compile_new(text) is None
    assert reads.compile_read(text) is None
    decision = route(text)
    assert decision.force_first_tool is None and not decision.expect_tool_first, decision.reason


def test_a_real_reminder_still_compiles():
    assert compile_task("remind me to call Mom tomorrow at 5pm", now=NOW) is not None


# --- P2: calendar shortcuts keep the whole request --------------------------------------

def args(text):
    compiled = reads.compile_read(text)
    return None if compiled is None else compiled[0][0][1]


def test_calendar_and_messages_is_not_answered_with_calendar_only():
    assert reads.compile_read("What's on my calendar and messages tomorrow?") is None
    assert reads.compile_read("Check my email and calendar for tomorrow") is None
    assert reads.compile_read("what's on my calendar and who emailed me") is None


def test_today_and_tomorrow_covers_both_days():
    assert args("What's on my calendar today and tomorrow?") == {"period": "today and tomorrow"}


def test_a_named_date_is_resolved_exactly(monkeypatch):
    assert reads._calendar_read_args("what's on my calendar for October 12", "", date(2026, 9, 30)) == {
        "period": "2026-10-12"}
    assert reads._calendar_read_args("show my calendar on 12th of oct", "", date(2026, 9, 30)) == {
        "period": "2026-10-12"}
    # A date already past this year is not guessed into next year, and an invalid one is refused.
    assert reads._calendar_read_args("what's on my calendar for March 3", "", date(2026, 9, 30)) is None
    assert reads._calendar_read_args("what's on my calendar for February 30", "", date(2026, 9, 30)) is None


@pytest.mark.parametrize("text", ["what's on my calendar next Friday", "show my calendar on the 12th",
                                  "what is on my calendar on Monday", "what's on my calendar in 3 weeks"])
def test_dates_the_shortcut_cannot_resolve_are_handed_on(text):
    assert reads.compile_read(text) is None, text


def test_a_reminder_exclusion_narrows_the_read():
    assert args("What's on my calendar this week, do not include reminders") == {
        "period": "this week", "calendar_only": True}
    decision = route("What's on my calendar this week, do not include reminders")
    assert decision.direct_calls and all(a.get("calendar_only") for n, a in decision.direct_calls
                                         if n == "get_upcoming"), decision.direct_calls


@pytest.mark.parametrize("text,expected", [
    ("what's on my calendar", {"days": 7}),
    ("what's on my calendar tomorrow", {"period": "tomorrow"}),
    ("what's on my calendar this week", {"period": "this week"}),
    ("show my schedule next week", {"period": "next week"})])
def test_supported_calendar_reads_keep_their_shortcut(text, expected):
    assert args(text) == expected


# --- P2: inbox shortcut does not swallow modifiers --------------------------------------

@pytest.mark.parametrize("text", ["Show my email purchases from Apple yesterday",
                                  "Show my email purchases from Apple and delete the old ones",
                                  "show my email purchases from Apple and archive them",
                                  "show my email purchases from Apple last week"])
def test_inbox_modifiers_are_not_flattened_into_the_sender(text):
    assert reads.compile_read(text) is None, text


def test_a_plain_sender_keeps_its_shortcut():
    assert args("show my email purchases from Apple") == {"query": "Apple", "strict_match": True}


# --- P2: authored prose does not force an unrelated Calendar read -----------------------

def test_authored_prose_does_not_force_a_calendar_read():
    decision = route("Email Sam asking to move our meeting")
    assert decision.force_first_tool is None, decision.reason
    assert "get_upcoming" not in {n for n, _ in decision.direct_calls}
    assert "send_email" in (decision.tool_subset or []) or "draft_email" in (decision.tool_subset or [])


def test_an_unresolved_topic_still_forces_the_lookup():
    # "email Mom about my move-in date" has no dictated content: the fact must be looked up first.
    assert route("email Mom about my move-in date").force_first_tool == "get_upcoming"


@pytest.mark.parametrize("text", [
    "Don't search my notes; is there anything about ucsc orientation I should know this week",
    "Don't check notes or messages; what's on my calendar tomorrow",
    "Do not email Sam, just text him I'm late", "Don't lock my screen but tell me my battery level"])
def test_a_leading_exclusion_with_a_real_request_is_not_a_prohibition(text):
    assert not U.is_prohibition(text), text


@pytest.mark.parametrize("text", ["Do not lock my screen, thanks", "Don't email Sam!"])
def test_a_trailing_pleasantry_does_not_hide_a_prohibition(text):
    assert U.is_prohibition(text), text


@pytest.mark.parametrize("text", ['Show calendar events named "Do not disturb" tomorrow',
                                  'show calendar events named "Lunch Oct 5 and messages" tomorrow'])
def test_words_inside_a_quoted_title_do_not_stop_a_supported_read(text):
    assert reads.compile_read(text) is not None, text


def test_active_calendar_filter_is_already_represented_by_the_tool():
    assert args("Show my calendar events that are not cancelled tomorrow") == {
        "period": "tomorrow", "calendar_only": True}


@pytest.mark.parametrize("text", [
    "Do not lock my screen, I am in the middle of a call.",
    "Do not lock my screen; just explain what that does.",
    "Do not read my clipboard, it contains private information.",
    "Do not run a speed test; just explain what it measures.",
])
def test_an_explanation_does_not_cancel_a_device_prohibition(text):
    assert U.is_prohibition(text)
    decision = route(text)
    assert decision.direct_calls == []
    assert decision.force_first_tool is None and not decision.expect_tool_first


@pytest.mark.parametrize("text", ['Tell me what "battery" means',
                                  "Why does my car battery keep dying?"])
def test_device_discussion_does_not_trigger_a_device_read(text):
    decision = route(text)
    assert decision.direct_calls == []
    assert decision.force_first_tool is None and not decision.expect_tool_first


@pytest.mark.parametrize("text", [
    "Check my calendar tomorrow and lock my screen.",
    "Check my calendar tomorrow; lock my screen.",
    "Check my calendar tomorrow, lock my screen.",
    "Lock my screen and check my calendar tomorrow.",
])
def test_calendar_and_device_actions_keep_both_tools_without_partial_dispatch(text):
    assert reads.compile_read(text) is None
    decision = route(text)
    assert decision.direct_calls == []
    assert {"get_upcoming", "lock_screen"} <= set(decision.tool_subset or ())
    assert decision.multi_round


@pytest.mark.parametrize("text", [
    "Lock my screen, and remind me to take my medicine tonight",
    "Lock my screen and remind me to take my medicine tonight",
    "Lock my screen; remind me to take my medicine tonight",
    "Remind me to take my medicine tonight, and lock my screen",
])
def test_compound_device_and_reminder_keeps_both_tools(text):
    assert compile_task(text, now=NOW) is None
    decision = route(text)
    assert decision.direct_calls == []
    assert {"lock_screen", "add_reminder"} <= set(decision.tool_subset or ())


@pytest.mark.parametrize("scope,days", [("the next 3 days", 3), ("next 7 days", 7),
                                       ("the next 7 days", 7), ("next 1 week", 7), ("next 45 days", 45),
                                       ("next 2 weeks", 14), ("next 60 days", 60)])
def test_supported_numeric_calendar_scope_is_preserved_at_both_layers(scope, days):
    text = "what is on my calendar for " + scope
    assert args(text) == {"days": days}
    assert route(text).direct_calls == [("get_upcoming", {"days": days})]


@pytest.mark.parametrize("scope", ["the next 0 days", "next 61 days", "next 9 weeks",
                                  "next 3 days and next week", "next 3 days and lock my screen"])
def test_numeric_calendar_shortcuts_do_not_truncate_or_drop_other_scope(scope):
    text = "what is on my calendar for " + scope
    assert reads.compile_read(text) is None
    assert route(text).direct_calls == []


def test_explicit_calendar_year_is_preserved_at_both_layers():
    text = "Show my calendar on October 12, 2027"
    assert reads._calendar_read_args(text, "", date(2026, 9, 30)) == {"period": "2027-10-12"}
    assert route(text).direct_calls == [("get_upcoming", {"period": "2027-10-12"})]


@pytest.mark.parametrize("text", [
    "Show my calendar from October 12 to October 15",
    "Show my calendar on October 12 and October 15",
    "Show my calendar tomorrow and next week",
])
def test_unresolved_calendar_scopes_decline_at_both_layers(text):
    assert reads.compile_read(text) is None
    assert route(text).direct_calls == []


@pytest.mark.parametrize("title", ["Do not disturb", "Lunch Oct 5 and messages"])
def test_calendar_title_is_passed_verbatim_at_both_layers(title):
    text = f'Show calendar events named "{title}" tomorrow'
    expected = {"period": "tomorrow", "query": title, "calendar_only": True}
    assert args(text) == expected
    assert route(text).direct_calls == [("get_upcoming", expected)]


@pytest.mark.parametrize("tail", ["yesterday", "and delete the old ones", "and archive them",
                                 "last week", "in my work account", "that are unread", "on Monday",
                                 "in October", "on 10/12"])
def test_final_router_inbox_shortcut_declines_unhandled_modifiers(tail):
    text = "Check my inbox for receipts from Apple " + tail
    assert R._email_search_query(text) is None
    assert route(text).direct_calls == []


def test_final_router_plain_inbox_search_keeps_its_shortcut():
    assert route("Check my inbox for receipts from Apple").direct_calls == [
        ("view_emails", {"query": "Apple", "count": 10, "strict_match": True})]


@pytest.fixture
def inert_endpoint(monkeypatch, tmp_path):
    """Real /agent, routing and loop; no inference engine or native tool bodies."""
    import httpx
    from service import main
    from service.agent import loop
    from service.memory import context
    from service.memory.store import SessionStore
    from service.tools.registry import REGISTRY
    from service.safety.policy import Tier, Decision

    streams, calls = [], []

    class Client:
        async def ensure_only(self, *_args, **_kwargs):
            pass

        async def chat(self, *_args, **_kwargs):
            raise AssertionError("Unexpected internal model call")

        async def stream_events(self, _model, messages, **kwargs):
            streams.append({"messages": messages, **kwargs})
            yield {"kind": "final", "message": {"role": "assistant", "content": "Synthetic explanation."}}

    async def no_engine():
        pass

    for name, original in tuple(REGISTRY.items()):
        async def tool(_name=name, **kwargs):
            calls.append((_name, kwargs))
            return "Synthetic tool result."
        monkeypatch.setitem(REGISTRY, name, replace(original, func=tool))
    store = SessionStore(tmp_path / "endpoint.db")
    monkeypatch.setattr(main, "client", Client(), raising=False)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(context, "store", store)
    monkeypatch.setattr(main, "ensure_omlx", no_engine)
    config = lambda: {"tool_retrieval": {"provider": "lexical"}}
    monkeypatch.setattr(main, "models_config", config)
    monkeypatch.setattr(R, "models_config", config)
    monkeypatch.setattr(loop, "audit", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "decide", lambda *_a, **_k: Decision(Tier.ALLOW, "synthetic only"))
    monkeypatch.setattr(reads, "decide", lambda *_a, **_k: Decision(Tier.ALLOW, "synthetic only"))

    async def request(prompt):
        # ASGI transport performs HTTP without a socket or app lifespan/native
        # startup. Root conftest isolates every import-time Wisp store.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://fixture.invalid") as client:
            response = await client.post("/agent", json={"prompt": prompt, "debug": False})
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert not [ev for ev in events if ev["type"] == "error"], events
        return events

    return request, streams, calls


@pytest.fixture
def successful_reminder_endpoint(inert_endpoint, monkeypatch, tmp_path):
    """Successful inert create plus real readback, not a failing tool stub."""
    from service import main
    from service.assistant.store import AssistantStore
    from service.tools.registry import REGISTRY

    request, streams, calls = inert_endpoint
    assistant = AssistantStore(tmp_path / "successful-reminders.db")
    monkeypatch.setattr(main, "assistant_store", assistant)

    async def persist_reminder(**kwargs):
        calls.append(("add_reminder", kwargs))
        assistant.add_manual(kwargs["title"], datetime.fromisoformat(
            kwargs["when_iso"]).timestamp(), kind=kwargs.get("kind", "reminder"))
        return "Reminder set: " + kwargs["title"]

    monkeypatch.setitem(REGISTRY, "add_reminder", replace(
        REGISTRY["add_reminder"], func=persist_reminder))
    yield request, streams, calls, assistant
    assistant._db.close()


@pytest.mark.parametrize("text", [
    "Lock my screen, and remind me to take my medicine tonight",
    "Lock my screen and remind me to take my medicine tonight",
    "Lock my screen; remind me to take my medicine tonight",
    "Remind me to take my medicine tonight, and lock my screen",
    "Remind me to take my medicine tonight; lock my screen",
])
def test_actual_http_reminder_compound_cannot_complete_only_the_reminder(
        successful_reminder_endpoint, monkeypatch, text):
    from service import main

    request, streams, calls, assistant = successful_reminder_endpoint

    async def interpret(_model, messages, **kwargs):
        streams.append({"messages": messages, "calls_before": len(calls), **kwargs})
        offered = {tool["function"]["name"] for tool in kwargs.get("tools", [])}
        pending = [name for name in ("lock_screen", "add_reminder")
                   if name in offered and name not in {name for name, _ in calls}]
        tool_calls = [{"id": "synthetic-" + name, "type": "function", "function": {
            "name": name, "arguments": json.dumps({"title": "take my medicine",
                "when_iso": "2026-10-02T20:00", "kind": "reminder"} if name == "add_reminder" else {})}}
            for name in pending]
        message = ({"role": "assistant", "content": "", "tool_calls": tool_calls} if pending
                   else {"role": "assistant", "content": "Synthetic compound handled."})
        yield {"kind": "final", "message": message}

    monkeypatch.setattr(main.client, "stream_events", interpret)
    events = asyncio.run(request(text))
    assert compile_task(text, now=NOW) is None
    assert {name for name, _ in calls} == {"lock_screen", "add_reminder"}
    assert len(calls) == 2
    assert assistant._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1
    assert not any(event["type"] == "task_plan" for event in events)
    assert streams and streams[0]["calls_before"] == 0
    offered = {tool["function"]["name"] for stream in streams for tool in stream["tools"]}
    assert {"lock_screen", "add_reminder"} <= offered
    assert events[-1]["type"] == "done"


@pytest.mark.parametrize("text,title", [
    ("Remind me to take my medicine tonight", "take my medicine"),
    ('Remind me to "buy milk and lock my screen" tomorrow', "buy milk and lock my screen"),
    ('Remind me to "call Mom and tell her the news" tomorrow', "call Mom and tell her the news"),
    ("Remind me to lock my screen tomorrow", "lock my screen"),
    ("Remind me to buy milk and eggs tomorrow", "buy milk and eggs"),
    ("can u send me a reminder to take my medicine tonight", "take my medicine"),
    ("**Create a reminder tomorrow to send my vaccine report to UCSC.**",
     "send my vaccine report to UCSC"),
])
def test_actual_http_supported_reminder_has_successful_temporary_readback(
        successful_reminder_endpoint, text, title):
    request, streams, calls, assistant = successful_reminder_endpoint
    events = asyncio.run(request(text))
    assert streams == []
    assert len(calls) == 1 and calls[0][0] == "add_reminder"
    assert calls[0][1]["title"] == title
    row = assistant._db.execute("SELECT title FROM commitments").fetchone()
    assert row[0] == title
    assert any(event["type"] == "tool_result" and event.get("status") == "succeeded"
               for event in events), events
    assert events[-1]["type"] == "done"


def test_actual_http_reminder_notification_preserves_supported_two_step_path(
        successful_reminder_endpoint):
    from service import main

    request, streams, calls, assistant = successful_reminder_endpoint
    text = "Remind me to take my medicine tonight and notify me"
    plan = compile_task(text, now=NOW)
    assert plan.parameters["notify_request"].value == "notify me"
    events = asyncio.run(request(text))
    assert len(calls) == 1 and calls[0][0] == "add_reminder"
    assert assistant._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1
    assert any(event["type"] == "tool_result" and event.get("status") == "succeeded"
               for event in events)
    assert streams == []
    workflow = main.store.latest_workflow(events[0]["id"])
    assert workflow["original_request"] == "notify me"
    assert workflow["artifact_provenance"] == "verified_tool_receipt"
    assert "Reminder set:" in workflow["artifact_text"]


@pytest.mark.parametrize("allow", [True, False])
def test_actual_http_explicit_reminder_notification_delivers_only_the_inert_receipt(
        successful_reminder_endpoint, monkeypatch, allow):
    from service import main
    from service.tools.registry import REGISTRY

    request, streams, calls, assistant = successful_reminder_endpoint
    approvals = []

    async def approve(_self, preview):
        approvals.append(preview)
        return allow

    async def inert_send(**kwargs):
        calls.append(("send_message", kwargs))
        return "Message sent to " + kwargs["to"] + ": synthetic receipt"

    monkeypatch.setattr(main.InteractiveApprover, "confirm", approve)
    monkeypatch.setitem(REGISTRY, "send_message", replace(REGISTRY["send_message"], func=inert_send))
    events = asyncio.run(request(
        "Remind me to take my medicine tonight and text +1 650 555 0134"))
    assert streams == []
    expected_calls = ["add_reminder", "send_message"] if allow else ["add_reminder"]
    assert [name for name, _ in calls] == expected_calls
    if allow:
        assert "Reminder set: take my medicine" in calls[1][1]["text"]
    assert approvals[0]["args"]["text"] == "Reminder set: take my medicine"
    assert len(approvals) == 1
    assert assistant._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1
    workflow = main.store.latest_workflow(events[0]["id"])
    assert workflow["status"] == ("completed" if allow else "cancelled")
    assert workflow["artifact_provenance"] == "verified_tool_receipt"
    assert events[-1]["type"] == "done"
    from service.workflows.models import WorkflowPlan
    from service.workflows.executor import execute_workflow

    loaded = WorkflowPlan.from_dict(workflow)
    assert loaded.status == ("completed" if allow else "cancelled")
    assert main.store.workflow_effect_claimed(loaded.id) is allow
    replay_events = []

    async def emit(event):
        replay_events.append(event)

    class NoApproval:
        async def confirm(self, *_a, **_k):
            raise AssertionError("completed receipt must not request another send")

    replay = asyncio.run(execute_workflow(loaded, emit, NoApproval(),
        store=main.store, session_id=events[0]["id"]))
    assert replay.status == "failed"
    assert [name for name, _ in calls] == expected_calls
    assert len(approvals) == 1
    assert not any(event["type"] == "tool_call" for event in replay_events)


@pytest.mark.parametrize("provenance", ["tool_receipt", "unknown", "user_claim"])
def test_notification_repair_does_not_promote_unverified_legacy_provenance(
        successful_reminder_endpoint, provenance):
    from service import main
    from service.workflows.models import WorkflowPlan
    from service.workflows.executor import execute_workflow

    with pytest.raises(ValueError, match="Unrecognized legacy artifact provenance"):
        WorkflowPlan.from_dict({"artifact_text": "Synthetic unverified text",
                                "artifact_provenance": provenance})
    # A directly constructed plan must not bypass the strict persisted loader.
    _request, _streams, calls, _assistant = successful_reminder_endpoint
    sid = main.store.create_session()
    plan = WorkflowPlan(artifact_text="Synthetic unverified text",
                        artifact_provenance=provenance, channel="messages",
                        recipient="+15555550123", status="running")
    main.store.save_workflow(sid, plan.to_dict())

    async def emit(_event):
        pass

    class NoApproval:
        async def confirm(self, *_a, **_k):
            raise AssertionError("unverified provenance must not reach approval")

    result = asyncio.run(execute_workflow(plan, emit, NoApproval(),
        store=main.store, session_id=sid))
    assert result.status == "failed"
    assert calls == []


@pytest.mark.parametrize("text", [
    "Remind me to take my medicine tonight and notify me and lock my screen",
    "Lock my screen; remind me to take my medicine tonight and notify me",
])
def test_notification_support_cannot_hide_a_third_action(text):
    assert compile_task(text, now=NOW) is None


def test_markdown_reminder_notification_keeps_the_complete_supported_task():
    plan = compile_task("**Create a reminder for me to finish a Canvas assignment by tonight "
                        "and tell Mom about it too through Messages.**", now=NOW)
    assert plan.intent == "reminder.create"
    assert plan.subject.value == "finish a Canvas assignment"
    assert plan.parameters["notify_request"].value.startswith("tell Mom")


@pytest.mark.parametrize("title", ["remind me", '"remind me"'])
def test_reminder_named_remind_me_is_still_an_existing_item_operation(title):
    plan = compile_task("delete the reminder named " + title, now=NOW)
    assert plan.intent == "reminder.delete"
    assert plan.target.value == "remind me"


def test_bare_addressed_message_keeps_reminder_words_in_its_literal_body():
    plan = compile_task("Text Mom remind me to buy bread tomorrow", now=NOW)
    assert plan.intent == "message.send"
    assert plan.subject.value == "remind me to buy bread tomorrow"
    assert plan.recipient.value == "Mom"


@pytest.mark.parametrize("text", [
    "Do not lock my screen, I am in the middle of a call.",
    "Do not lock my screen; just explain what that does.",
    "Do not read my clipboard, it contains private information.",
    "Do not run a speed test; just explain what it measures.",
    'Tell me what "battery" means', "Why does my car battery keep dying?",
])
def test_actual_http_device_denial_or_discussion_executes_nothing(inert_endpoint, text):
    request, streams, calls = inert_endpoint
    asyncio.run(request(text))
    assert calls == []
    assert streams and all(s.get("tool_choice", "auto") == "auto" for s in streams)


@pytest.mark.parametrize("text", ["Check my calendar tomorrow and lock my screen.",
                                  "Check my calendar tomorrow; lock my screen."])
def test_actual_http_calendar_compound_reaches_interpretation_with_both_tools(inert_endpoint, text):
    request, streams, calls = inert_endpoint
    asyncio.run(request(text))
    assert calls == []
    assert streams and streams[0].get("tool_choice", "auto") == "auto"
    offered = {tool["function"]["name"] for tool in streams[0]["tools"]}
    assert {"get_upcoming", "lock_screen"} <= offered


@pytest.mark.parametrize("text,expected", [
    ("What's my battery level", ("get_battery_status", {})),
    ('Show calendar events named "Do not disturb" tomorrow',
     ("get_upcoming", {"period": "tomorrow", "query": "Do not disturb", "calendar_only": True})),
    ("Show my calendar on October 12, 2027", ("get_upcoming", {"period": "2027-10-12"})),
])
def test_actual_http_positive_controls_detect_the_real_dispatch(inert_endpoint, text, expected):
    request, _streams, calls = inert_endpoint
    asyncio.run(request(text))
    assert calls == [expected]


@pytest.mark.parametrize("text", ["Check my inbox for receipts from Apple yesterday",
                                  "Check my inbox for receipts from Apple and delete the old ones",
                                  "Show my calendar on October 12 and October 15"])
def test_actual_http_unresolved_scope_reaches_the_model_without_partial_reads(inert_endpoint, text):
    request, streams, calls = inert_endpoint
    asyncio.run(request(text))
    assert calls == []
    assert streams and any(text in message.get("content", "") for message in streams[0]["messages"])


@pytest.mark.parametrize("text,expected_title", [
    ('Show calendar events named "Do not disturb" tomorrow', "Do not disturb"),
    ("Show my calendar on October 12, 2027", "Exact future year"),
])
def test_actual_calendar_read_filters_temporary_store_by_title_and_exact_year(monkeypatch, tmp_path, text, expected_title):
    from unittest.mock import AsyncMock
    from service.assistant.store import AssistantStore
    from service.assistant import sync_status
    from service.tools import assistant_tools, timeranges

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    store = AssistantStore(tmp_path / "calendar.db")
    store.sync_source("calendar", [
        {"source_id": "title", "kind": "event", "title": "Do not disturb",
         "when_ts": datetime(2026, 10, 1, 12).timestamp()},
        {"source_id": "other", "kind": "event", "title": "Unrelated calendar entry",
         "when_ts": datetime(2026, 10, 1, 13).timestamp()},
        {"source_id": "wrong-year", "kind": "event", "title": "Current year decoy",
         "when_ts": datetime(2026, 10, 12, 12).timestamp()},
        {"source_id": "exact-year", "kind": "event", "title": "Exact future year",
         "when_ts": datetime(2027, 10, 12, 12).timestamp()},
    ])
    store.sync_source("reminders", [{"source_id": "reminder", "kind": "reminder",
        "title": "Do not disturb reminder", "when_ts": datetime(2026, 10, 1, 14).timestamp()}])
    ready = {"sources": [{"id": "calendar", "label": "Calendar", "state": "available"},
                          {"id": "reminders", "label": "Reminders", "state": "available"}]}
    monkeypatch.setattr(assistant_tools, "assistant_store", store)
    monkeypatch.setattr(assistant_tools.time, "time", lambda: NOW.timestamp())
    monkeypatch.setattr(reads, "datetime", Clock)
    monkeypatch.setattr(timeranges, "datetime", Clock)
    monkeypatch.setattr(sync_status, "ensure_sources", AsyncMock(return_value=ready))

    async def emit(_event):
        pass

    try:
        result = asyncio.run(reads.execute_read(reads.compile_read(text), emit))
        assert result.status == "completed"
        assert expected_title in result.response
        assert "Unrelated calendar entry" not in result.response
        assert "Current year decoy" not in result.response
        assert "Do not disturb reminder" not in result.response
    finally:
        store._db.close()
