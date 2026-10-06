"""Actual main.agent orchestration with isolated sessions and synthetic tools."""
from __future__ import annotations
import asyncio
import json
import socket
import subprocess
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from service import main
from service.memory import context
from service.memory.store import SessionStore
from service.router import router
from service.router.intent import planner
from service.tools.registry import REGISTRY
from tests.test_router_intent_core import FakeClient, CONFIG, MODEL, TARGET, source, value, REFERENCE_READ_CASES, LITERAL_SOURCE_CASES, PREPOSITION_READ_CASES, GOVERNED_TITLE_CASES, ACTION_TAIL_CASES, NEGATIVE_EFFECT_TAILS, LATER_EFFECT_CASES, READ_AFTER_NEGATIVE_JOINERS


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    client = FakeClient()
    config = {"intent_router": CONFIG, "tool_retrieval": {"provider": "lexical"}}
    calls = []
    monkeypatch.setattr(main, "client", client, raising=False)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(context, "store", store)
    monkeypatch.setattr(main, "models_config", lambda: config)
    monkeypatch.setattr(router, "role_to_model", lambda role: MODEL)
    monkeypatch.setattr(planner, "role_target", lambda role: TARGET if role == "router" else None)
    monkeypatch.setattr(main, "ensure_omlx", AsyncMock(side_effect=AssertionError("no startup")))
    monkeypatch.setattr(main, "maybe_summarize", AsyncMock())
    def forbidden(*a, **k):
        raise AssertionError("Synthetic intent tests may not run sockets or native tools")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for name, tool in list(REGISTRY.items()):
        def fixture(*, _name=name, **args):
            calls.append((_name, args))
            if _name == "summarize_emails":
                return "Inbox: synthetic Acme update."
            if _name == "summarize_messages":
                return "Texts: you replied to Imani. One conversation could not be checked."
            if _name == "get_upcoming":
                return "Monday: synthetic planning. Work calendar could not be checked."
            if _name in {"view_emails", "view_messages", "search_reminders", "find_free_time"}:
                return "Synthetic exact fixture records."
            if _name == "search_notes":
                return "Synthetic note: " + str(args.get("query"))
            return "(error: unexpected fixture tool)"
        monkeypatch.setitem(REGISTRY, name, replace(tool, func=fixture))
    async def request(prompt, **kw):
        response = await main.agent({"prompt": prompt, "debug": False, **kw})
        events = []
        async for item in response.body_iterator:
            if isinstance(item, bytes):
                item = item.decode()
            events.append(json.loads(item.removeprefix("data: ").strip()))
        assert not [event for event in events if event["type"] == "error"], events
        return events
    return request, client, calls, store, config


def text(events):
    return "\n".join(event.get("text", "") for event in events if event["type"] == "text")


def test_multisource_compilation_survives_pinned_coding_session(endpoint):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    store.set_pinned(sid, "coding", "wrong-pinned-model")
    client.outputs = [value(source("email"), source("messages"))]
    events = asyncio.run(request("Recap my email and texts", session_id=sid))
    assert calls == [("summarize_emails", {}), ("summarize_messages", {})]
    assert client.calls == ["status", "chat"]
    assert text(events) == "**Email:**\nInbox: synthetic Acme update.\n\n**Messages:**\nTexts: you replied to Imani. One conversation could not be checked."
    assert next(event for event in events if event["type"] == "routed")["intent_disposition"] == "compiled"


def test_invalid_output_cannot_fan_out_or_read_excluded_sources(endpoint):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(source("notes")), value(source("notes"))]
    events = asyncio.run(request("Recap my email without notes"))
    assert not calls and client.calls == ["status", "chat", "status", "chat"]
    assert "could not validate" in text(events)


def test_unavailable_model_clarifies_without_engine_start_or_private_read(endpoint):
    request, client, calls, _, _ = endpoint
    client.loaded = False
    events = asyncio.run(request("Recap my email and texts"))
    assert not calls and client.calls == ["status"]
    assert "unavailable" in text(events)


def test_exact_weekly_fast_path_avoids_all_model_calls(endpoint):
    request, client, calls, _, _ = endpoint
    events = asyncio.run(request("What is up for this week"))
    assert calls == [("get_upcoming", {"period": "this week"})]
    assert not client.calls
    assert "Work calendar could not be checked" in text(events)


def test_filtered_overview_is_not_silently_downgraded_by_earlier_compile_read(endpoint):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(source("email", query="Acme"))]
    events = asyncio.run(request("Show my email summary from Acme"))
    assert not calls and client.calls == ["status", "chat"]
    assert "query filter" in text(events)


def test_distinct_note_scopes_keep_both_calls_and_outputs(endpoint):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(source("notes", "records", query="Alpha"), source("notes", "records", query="Beta"))]
    events = asyncio.run(request('Find notes named "Alpha" and "Beta"'))
    assert calls == [("search_notes", {"query": "Alpha"}), ("search_notes", {"query": "Beta"})]
    assert "Synthetic note: Alpha" in text(events) and "Synthetic note: Beta" in text(events)
    ids = [event["id"] for event in events if event["type"] == "tool_call"]
    assert len(ids) == len(set(ids)) == 2


def test_dry_run_never_issues_planner_generation(endpoint):
    request, client, calls, _, _ = endpoint
    # A complete deterministic fast path is enough to exercise dry-run contract.
    events = asyncio.run(request("What is up for this week", test_mode=True))
    assert not client.calls and not calls
    assert "Dry run only" in text(events)


@pytest.mark.parametrize("prompt,scope", [
    ("whats up for the week", "this week"),
    ("how's my week looking", "this week"),
    ("anything coming up for me", None),
    ("my calender this wk", "this week"),
])
def test_flexible_weekly_eligibility_runs_real_endpoint_with_bounded_fake_planner(endpoint, prompt, scope):
    request, client, calls, _, _ = endpoint
    time = {"time": {"named": scope}} if scope else {}
    # Misspelled explicit calendar source is still one requested domain;
    # implicit personal agenda may include both explicitly declared sources.
    sources = [source("calendar", **time)]
    if "calender" not in prompt:
        sources.append(source("reminders", **time))
    client.outputs = [value(*sources)]
    events = asyncio.run(request(prompt))
    assert client.calls == ["status", "chat"]
    assert calls and calls[0][0] == "get_upcoming"
    assert "compiled" in [event.get("intent_disposition") for event in events]


def test_contextual_domain_cannot_be_replaced_by_model(endpoint):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    store.add_turn(sid, "user", "Recap my email")
    store.add_turn(sid, "assistant", "Synthetic email digest", tool_digest="summarize_emails")
    client.outputs = [value(source("calendar", time={"named": "tomorrow"})), value(source("calendar", time={"named": "tomorrow"}))]
    events = asyncio.run(request("Same for tomorrow", session_id=sid))
    assert not calls and "could not validate" in text(events)


@pytest.mark.parametrize("mutation", [
    lambda c: setattr(c, "base_url", "https://remote.example"),
    lambda c: setattr(c, "provider", None),
    lambda c: setattr(c, "_credential_transport", None),
])
def test_actor_identity_mismatch_has_no_status_generation_or_source_execution(endpoint, mutation):
    request, client, calls, _, _ = endpoint
    mutation(client)
    events = asyncio.run(request("Recap my email and texts"))
    assert not client.calls and not calls
    routed = next(e for e in events if e["type"] == "routed")
    assert routed["intent_disposition"] == "clarify" and routed["model"] == ""


def test_actor_reports_router_model_instead_of_fast_default(endpoint, monkeypatch):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(source("email"), source("messages"))]
    monkeypatch.setattr(router, "role_to_model", lambda role: "other-fast-default")
    events = asyncio.run(request("Recap my email and texts"))
    assert next(e for e in events if e["type"] == "routed")["model"] == TARGET.model
    assert client.calls == ["status", "chat"] and len(calls) == 2


@pytest.mark.parametrize("prompt", [
    "Summarize recent text messages.",
    "What did people text me?",
    "Show my recent text summary.",
])
def test_message_read_reaches_actor_without_delivery_workflow(endpoint, prompt):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    client.outputs = [value(source("messages"))]
    events = asyncio.run(request(prompt, session_id=sid))
    assert calls == [("summarize_messages", {})]
    assert "Texts: you replied to Imani." in text(events)
    assert "what to send" not in text(events) and "I couldn't tell" not in text(events)
    assert store.latest_workflow(sid) is None
    assert not any("workflow" in e["type"] for e in events)


@pytest.mark.parametrize("prompt,expected", [
    ("what is up tomorrow?", {"period": "tomorrow", "calendar_only": True}),
    ("show my calendar this week", {"period": "this week", "calendar_only": True}),
    ("what is up for this week?", {"period": "this week"}),
])
def test_actor_exact_shortcut_preserves_calendar_only_authority(endpoint, prompt, expected):
    request, client, calls, _, _ = endpoint
    events = asyncio.run(request(prompt))
    assert calls == [("get_upcoming", expected)]
    assert not client.calls
    assert "Work calendar could not be checked" in text(events)


@pytest.mark.parametrize("prompt,bad", [
    ("Read email for 2026-10-01", source("email", "records")),
    ("Read email for October 2026", source("email", "records")),
    ("Read overdue reminders", source("reminders")),
    ("Read overdue reminders", source("reminders", scope="all")),
    ("Find a 90 minute free slot on my calendar tomorrow", source("calendar", "free_time", time={"named": "tomorrow"})),
    ("Find a 90 minute free slot on my calendar tomorrow", source("calendar", "free_time", time={"named": "tomorrow"}, minutes=30)),
    ("Find notes about audit blueprints", source("notes", "records", query="audit")),
])
def test_actor_rejects_omitted_or_wrong_constraints_before_source_execution(endpoint, prompt, bad):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(bad), value(bad)]
    events = asyncio.run(request(prompt))
    assert not calls
    assert next(e for e in events if e["type"] == "routed")["intent_disposition"] == "clarify"


@pytest.mark.parametrize("prompt,good,expected", [
    ("Read email for 2026-10-01", source("email", "records", time={"date": "2026-10-01"}), ("view_emails", {"period": "2026-10-01"})),
    ("Read email for October 2026", source("email", "records", time={"month": "2026-10"}), ("view_emails", {"period": "2026-10"})),
    ("Read overdue reminders", source("reminders", scope="overdue"), ("search_reminders", {"query": "", "scope": "past_due"})),
    ("Find a 90 minute free slot on my calendar tomorrow", source("calendar", "free_time", time={"named": "tomorrow"}, minutes=90), ("find_free_time", {"period": "tomorrow", "minutes": 90})),
    ("Find notes about audit blueprints", source("notes", "records", query="audit blueprints"), ("search_notes", {"query": "audit blueprints"})),
])
def test_actor_executes_exact_supported_constraints_without_default_broadening(endpoint, prompt, good, expected):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(good)]
    events = asyncio.run(request(prompt))
    assert calls == [expected] and "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("prompt,data,expected", [
    ("Read email today plus messages yesterday", value(source("email", "records", time={"named": "today"}), source("messages", "records", time={"named": "yesterday"})),
     [("view_emails", {"period": "today"}), ("view_messages", {"period": "yesterday"})]),
    ("Find notes about audit blueprints from yesterday limit to five", value(source("notes", "records", query="audit blueprints", time={"named": "yesterday"}, count=5)),
     [("search_notes", {"query": "audit blueprints", "count": 5, "period": "yesterday"})]),
])
def test_actor_preserves_independent_dates_and_complete_query_with_limit(endpoint, prompt, data, expected):
    request, client, calls, _, _ = endpoint
    client.outputs = [data]
    events = asyncio.run(request(prompt))
    assert calls == expected and "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("swapped", [False, True])
def test_actor_binds_each_repeated_email_query_to_its_requested_date(endpoint, swapped):
    from tests.test_router_intent_core import REPEATED_READ_PROMPT, repeated_email
    request, client, calls, _, _ = endpoint
    data = repeated_email(swapped=swapped)
    client.outputs = [data, data]
    events = asyncio.run(request(REPEATED_READ_PROMPT))
    if swapped:
        assert calls == []
        assert "clarify" in [e.get("intent_disposition") for e in events]
    else:
        assert calls == [("view_emails", {"query": "Elara", "strict_match": True, "period": "2026-10-03"}),
                         ("view_emails", {"query": "Tobias", "strict_match": True, "period": "2026-10-04"})]
        assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("swap", [None, "account", "count", "time", "operation", "unread"])
def test_actor_whole_read_binding_preserves_account_count_operation_and_unread(endpoint, swap):
    request, client, calls, _, _ = endpoint
    prompt = ('Read two unread emails from Elara in account "Work" for 2026-10-03; '
              'read three emails from Tobias in account "Personal" for 2026-10-04')
    data = value(source("email", "records", query="Elara", account="Work", count=2, unread=True, time={"date": "2026-10-03"}),
                 source("email", "records", query="Tobias", account="Personal", count=3, time={"date": "2026-10-04"}))
    if swap in {"account", "count", "time"}:
        data["sources"][0][swap], data["sources"][1][swap] = data["sources"][1][swap], data["sources"][0][swap]
    elif swap == "operation":
        data["sources"][0]["operation"] = "overview"
    elif swap == "unread":
        data["sources"][0].pop("unread")
        data["sources"][1]["unread"] = True
    client.outputs = [data, data]
    events = asyncio.run(request(prompt))
    if swap:
        assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]
    else:
        assert calls == [("view_emails", {"query": "Elara", "strict_match": True, "account": "Work", "count": 2, "unread": True, "period": "2026-10-03"}),
                         ("view_emails", {"query": "Tobias", "strict_match": True, "account": "Personal", "count": 3, "period": "2026-10-04"})]
        assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("query", ["cedar manuscripts", "birch diagrams"])
def test_actor_source_free_replacement_never_executes_prior_query(endpoint, query):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    store.add_turn(sid, "user", "Find five notes about cedar manuscripts from yesterday")
    store.add_turn(sid, "assistant", "Synthetic prior notes", tool_digest="search_notes")
    data = value(source("notes", "records", query=query, count=5, time={"named": "yesterday"}))
    client.outputs = [data, data]
    events = asyncio.run(request("Actually about birch diagrams", session_id=sid))
    if query == "cedar manuscripts":
        assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]
    else:
        assert calls == [("search_notes", {"query": "birch diagrams", "count": 5, "period": "yesterday"})]
        assert "compiled" in [e.get("intent_disposition") for e in events]


def test_actor_ambiguous_source_free_replacement_does_not_choose_a_source(endpoint):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    store.add_turn(sid, "user", "Find notes about cedar manuscripts and read email from Elara")
    store.add_turn(sid, "assistant", "Synthetic prior reads", tool_digest="search_notes, view_emails")
    data = value(source("notes", "records", query="birch diagrams"), source("email", "records", query="Elara"))
    client.outputs = [data, data]
    events = asyncio.run(request("Actually about birch diagrams", session_id=sid))
    assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]



def test_actor_repeated_context_date_correction_keeps_queries_and_changes_every_date(endpoint):
    from tests.test_router_intent_core import REPEATED_READ_PROMPT
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    store.add_turn(sid, "user", REPEATED_READ_PROMPT)
    store.add_turn(sid, "assistant", "Synthetic prior email reads", tool_digest="view_emails")
    data = value(source("email", "records", query="Elara", time={"named": "tomorrow"}),
                 source("email", "records", query="Tobias", time={"named": "tomorrow"}))
    client.outputs = [data]
    events = asyncio.run(request("Same for tomorrow", session_id=sid))
    assert calls == [("view_emails", {"query": "Elara", "strict_match": True, "period": "tomorrow"}),
                     ("view_emails", {"query": "Tobias", "strict_match": True, "period": "tomorrow"})]
    assert "compiled" in [e.get("intent_disposition") for e in events]



@pytest.mark.parametrize("prompt,good,expected", REFERENCE_READ_CASES)
def test_actor_source_reference_aliases_execute_one_faithful_read(endpoint, prompt, good, expected):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(good)]
    events = asyncio.run(request(prompt))
    assert calls == [expected]
    assert "compiled" in [e.get("intent_disposition") for e in events]


def test_actor_referential_aliases_do_not_erase_independent_read_scopes(endpoint):
    request, client, calls, _, _ = endpoint
    prompt = "Read appointments on my calendar tomorrow; read appointments in the calendar next week"
    bad = value(source("calendar", "records", time={"named": "tomorrow"}))
    client.outputs = [bad, bad]
    events = asyncio.run(request(prompt))
    assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]



def test_actor_unclear_repeated_source_boundaries_clarify_despite_complete_date_set(endpoint):
    request, client, calls, _, _ = endpoint
    bad = value(source("calendar", "records", time={"named": "tomorrow"}), source("calendar", "records", time={"named": "next week"}))
    client.outputs = [bad, bad]
    events = asyncio.run(request("Read appointments tomorrow calendar next week"))
    assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]



@pytest.mark.parametrize("case", ["mixed", "coordinated"])
def test_actor_supported_mixed_lookup_and_coordinated_sender_reads(endpoint, case):
    from tests.test_router_intent_core import MIXED_LOOKUP_PROMPT, MIXED_LOOKUP_GOOD, COORDINATED_EMAIL_PROMPT, COORDINATED_EMAIL_GOOD
    request, client, calls, _, _ = endpoint
    prompt, data = (MIXED_LOOKUP_PROMPT, MIXED_LOOKUP_GOOD) if case == "mixed" else (COORDINATED_EMAIL_PROMPT, COORDINATED_EMAIL_GOOD)
    client.outputs = [data]
    events = asyncio.run(request(prompt))
    expected = [("get_upcoming", {"period": "tomorrow", "calendar_only": True}), ("search_notes", {"query": "amber notebooks", "period": "yesterday"})] if case == "mixed" else [
        ("view_emails", {"query": "Selene", "strict_match": True, "period": "yesterday"}),
        ("view_emails", {"query": "Dorian", "strict_match": True, "period": "yesterday"})]
    assert calls == expected
    assert client.calls == ["status", "chat"]
    assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("bad_query", [None, "amber"])
def test_actor_mixed_lookup_still_rejects_missing_or_truncated_filter(endpoint, bad_query):
    from tests.test_router_intent_core import MIXED_LOOKUP_PROMPT
    request, client, calls, _, _ = endpoint
    query = {"query": bad_query} if bad_query else {}
    bad = value(source("calendar", "records", time={"named": "tomorrow"}), source("notes", "records", time={"named": "yesterday"}, **query))
    client.outputs = [bad, bad]
    events = asyncio.run(request(MIXED_LOOKUP_PROMPT))
    assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("tail", ["email Mom", "text Mom", "forward it by email to Mom"])
def test_actor_true_delivery_keeps_read_planner_guard(endpoint, monkeypatch, tail):
    import service.router.intent as intent_api
    request, client, calls, _, _ = endpoint
    planner_spy = AsyncMock(side_effect=AssertionError("delivery cannot enter read planner"))
    monkeypatch.setattr(intent_api, "plan_read", planner_spy)
    async def fake_agent(_client, _model, _messages, emit, _approver, **kwargs):
        await emit({"type": "text", "text": "Synthetic guarded action path."})
        return "Synthetic guarded action path."
    monkeypatch.setattr(main, "run_agent", fake_agent)
    events = asyncio.run(request("Read email from Selene and " + tail))
    planner_spy.assert_not_awaited()
    assert not client.calls
    assert "compiled" not in [e.get("intent_disposition") for e in events]



def test_actor_quoted_coordinated_senders_keep_exact_filters_and_shared_date(endpoint):
    from tests.test_router_intent_core import COORDINATED_EMAIL_GOOD
    request, client, calls, _, _ = endpoint
    client.outputs = [COORDINATED_EMAIL_GOOD]
    events = asyncio.run(request('Read email from "Selene" and email from "Dorian" for yesterday'))
    assert calls == [("view_emails", {"query": "Selene", "strict_match": True, "period": "yesterday"}),
                     ("view_emails", {"query": "Dorian", "strict_match": True, "period": "yesterday"})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


def test_actor_mixed_lookup_date_swap_cannot_execute(endpoint):
    request, client, calls, _, _ = endpoint
    bad = value(source("calendar", "records", time={"named": "yesterday"}), source("notes", "records", query="amber notebooks", time={"named": "tomorrow"}))
    client.outputs = [bad, bad]
    events = asyncio.run(request("Show my calendar tomorrow; find notes about amber notebooks from yesterday"))
    assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]



@pytest.mark.parametrize("misbound", [False, True])
def test_actor_lookup_filter_binds_to_one_repeated_source_occurrence(endpoint, misbound):
    request, client, calls, _, _ = endpoint
    first_query = {"query": "amber notebooks"} if misbound else {}
    second_query = {} if misbound else {"query": "amber notebooks"}
    data = value(source("notes", "records", time={"named": "tomorrow"}, **first_query),
                 source("notes", "records", time={"named": "yesterday"}, **second_query))
    client.outputs = [data, data]
    events = asyncio.run(request("Show my notes tomorrow; find notes about amber notebooks from yesterday"))
    if misbound:
        assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]
    else:
        assert calls == [("search_notes", {"period": "tomorrow"}), ("search_notes", {"query": "amber notebooks", "period": "yesterday"})]
        assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("head", ["Recap", "Review", "Inspect", "Scan"])
@pytest.mark.parametrize("mixed", [False, True])
def test_actor_read_head_does_not_enter_delivery_workflow(endpoint, head, mixed):
    request, client, calls, _, _ = endpoint
    data = value(source("email"))
    prompt = head + " email"
    expected = [("summarize_emails", {})]
    if mixed:
        prompt += "; find notes named harbor sketches"
        data["sources"].append(source("notes", "records", query="harbor sketches"))
        expected.append(("search_notes", {"query": "harbor sketches"}))
    client.outputs = [data]
    events = asyncio.run(request(prompt))
    assert calls == expected
    assert "compiled" in [e.get("intent_disposition") for e in events]
    assert not [e for e in events if e.get("workflow_state") == "waiting_for_content"]


@pytest.mark.parametrize("prompt,query", LITERAL_SOURCE_CASES)
@pytest.mark.parametrize("bad", [False, True])
def test_actor_literal_source_words_keep_exact_single_source_scope(endpoint, prompt, query, bad):
    request, client, calls, _, _ = endpoint
    data = value(source("notes", "records", query=query))
    if bad:
        data["sources"].append(source("calendar"))
    client.outputs = [data, data]
    events = asyncio.run(request(prompt))
    assert calls == ([] if bad else [("search_notes", {"query": query})])
    assert ("clarify" if bad else "compiled") in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("omit", [False, True])
def test_actor_lookup_source_literal_keeps_trailing_exclusion(endpoint, omit):
    request, client, calls, _, _ = endpoint
    data = value(source("notes", "records", query="my calendar"))
    data["excluded_sources"] = [] if omit else ["email"]
    client.outputs = [data, data]
    events = asyncio.run(request("Find notes about my calendar without email"))
    assert calls == ([] if omit else [("search_notes", {"query": "my calendar"})])
    assert ("clarify" if omit else "compiled") in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("prompt,query", PREPOSITION_READ_CASES)
def test_actor_coordinated_query_preposition_executes_both_exact_reads(endpoint, prompt, query):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(source("email", "records", query="Lyra"), source("email", "records", query=query))]
    events = asyncio.run(request(prompt))
    assert calls == [("view_emails", {"query": "Lyra", "strict_match": True}),
                     ("view_emails", {"query": query, "strict_match": True})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("prompt", ["Recap email and email Mom the summary",
    "Review email and text Mom saying hello", "Recap email and draft an email to Mom",
    "Read email from Lyra and email about road to recovery to Mom",
    "Read email from Lyra and email about roadmap to person@example.com",
    "Read email from Lyra and email about road to recovery and send it by email to Mom"])
def test_actor_read_head_and_attachment_repairs_preserve_delivery_guard(endpoint, monkeypatch, prompt):
    import service.router.intent as intent_api
    request, client, calls, _, _ = endpoint
    planner_spy = AsyncMock(side_effect=AssertionError("delivery cannot enter read planner"))
    monkeypatch.setattr(intent_api, "plan_read", planner_spy)
    async def fake_agent(_client, _model, _messages, emit, _approver, **kwargs):
        await emit({"type": "text", "text": "Synthetic guarded action path."})
        return "Synthetic guarded action path."
    monkeypatch.setattr(main, "run_agent", fake_agent)
    events = asyncio.run(request(prompt))
    planner_spy.assert_not_awaited()
    assert not client.calls and "compiled" not in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("wrong_source", [False, True])
def test_actor_prior_literal_source_word_never_grants_contextual_authority(endpoint, wrong_source):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    store.add_turn(sid, "user", "Find notes about my calendar")
    store.add_turn(sid, "assistant", "Synthetic notes", tool_digest="search_notes")
    data = value(source("calendar", time={"named": "yesterday"})) if wrong_source else value(
        source("notes", "records", query="my calendar", time={"named": "yesterday"}))
    client.outputs = [data, data]
    events = asyncio.run(request("Same for yesterday", session_id=sid))
    assert calls == ([] if wrong_source else [("search_notes", {"query": "my calendar", "period": "yesterday"})])
    assert ("clarify" if wrong_source else "compiled") in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("bad_query", [None, "my"])
def test_actor_literal_source_lookup_cannot_omit_or_truncate_query(endpoint, bad_query):
    request, client, calls, _, _ = endpoint
    data = value(source("notes", "records", **({"query": bad_query} if bad_query else {})))
    client.outputs = [data, data]
    events = asyncio.run(request("Find notes about my calendar"))
    assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("omit", [False, True])
def test_actor_literal_source_word_preserves_independent_email_read(endpoint, omit):
    request, client, calls, _, _ = endpoint
    data = value(source("notes", "records", query="my calendar"))
    if not omit:
        data["sources"].append(source("email", "records", query="Lyra"))
    client.outputs = [data, data]
    events = asyncio.run(request("Find notes about my calendar and read email from Lyra"))
    assert calls == ([] if omit else [("search_notes", {"query": "my calendar"}),
                                     ("view_emails", {"query": "Lyra", "strict_match": True})])
    assert ("clarify" if omit else "compiled") in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("head", ["Recap", "Review"])
def test_actor_message_read_heads_reach_read_execution(endpoint, head):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(source("messages"))]
    events = asyncio.run(request(head + " text"))
    assert calls == [("summarize_messages", {})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


def test_actor_single_domain_allowlist_keeps_query_literal_and_exclusion(endpoint):
    request, client, calls, _, config = endpoint
    config["intent_router"] = {**CONFIG, "domains": ["notes"]}
    data = value(source("notes", "records", query="my calendar"))
    data["excluded_sources"] = ["email"]
    client.outputs = [data]
    events = asyncio.run(request("Find notes about my calendar without email"))
    assert calls == [("search_notes", {"query": "my calendar"})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("prompt,query", GOVERNED_TITLE_CASES)
def test_actor_capitalized_governed_title_executes_exact_reads_without_recipient(endpoint, prompt, query):
    request, client, calls, _, _ = endpoint
    first = source("notes", "records", query="maps") if prompt.startswith("Find") else source("email", "records", query="Cassia")
    client.outputs = [value(first, source("email", "records", query=query))]
    events = asyncio.run(request(prompt))
    first_call = ("search_notes", {"query": "maps"}) if prompt.startswith("Find") else ("view_emails", {"query": "Cassia", "strict_match": True})
    assert calls == [first_call, ("view_emails", {"query": query, "strict_match": True})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("joiner,tail", ACTION_TAIL_CASES)
@pytest.mark.parametrize("swallowed", [False, True])
def test_actor_later_effect_never_executes_a_swallowed_or_partial_read(endpoint, monkeypatch, joiner, tail, swallowed):
    import service.router.intent as intent_api
    request, client, calls, _, _ = endpoint
    prompt = "Find notes about cedar maps " + joiner + " " + tail
    query = "cedar maps " + joiner + " " + tail if swallowed else "cedar maps"
    client.outputs = [value(source("notes", "records", query=query))] * 2
    planner_spy = AsyncMock(wraps=intent_api.plan_read)
    monkeypatch.setattr(intent_api, "plan_read", planner_spy)
    async def fake_agent(_client, _model, _messages, emit, _approver, **kwargs):
        await emit({"type": "text", "text": "Synthetic guarded action path."})
        return "Synthetic guarded action path."
    monkeypatch.setattr(main, "run_agent", fake_agent)
    events = asyncio.run(request(prompt))
    assert not calls and not client.calls
    assert "compiled" not in [e.get("intent_disposition") for e in events]
    # Delivery heads are barred before planning. Other established local
    # effects may reach plan_read, whose applicability must decline them.
    if any(verb in tail for verb in ["share", "send", "forward"]):
        planner_spy.assert_not_awaited()


def test_actor_quoted_share_is_data_and_later_explicit_share_remains_action(endpoint):
    request, client, calls, _, _ = endpoint
    query = "cedar maps and share it with Mom"
    client.outputs = [value(source("notes", "records", query=query))]
    events = asyncio.run(request('Find notes about "' + query + '"'))
    assert calls == [("search_notes", {"query": query})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("tail", NEGATIVE_EFFECT_TAILS)
def test_actor_negative_effect_never_becomes_part_of_executed_query(endpoint, tail):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    client.outputs = [value(source("notes", "records", query="cedar maps"))]
    events = asyncio.run(request("Find notes about cedar maps and " + tail, session_id=sid))
    assert calls == [("search_notes", {"query": "cedar maps"})]
    assert text(events) == "Synthetic note: cedar maps"
    assert "compiled" in [event.get("intent_disposition") for event in events]
    assert store.latest_workflow(sid) is None
    assert not any("workflow" in event["type"] for event in events)
    assert not [event for event in events if event.get("tool") in {"send_email", "send_message"}]


def test_actor_unquoted_action_words_under_one_filter_remain_exact_data(endpoint):
    request, client, calls, _, _ = endpoint
    client.outputs = [value(source("notes", "records", query="share it with Mom"))]
    events = asyncio.run(request("Find notes about share it with Mom"))
    assert calls == [("search_notes", {"query": "share it with Mom"})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("query", ["Guide to Gardening to Taylor", "Guide to Gardening"])
def test_actor_ambiguous_second_named_destination_executes_neither_read_nor_send(endpoint, query):
    request, client, calls, _, _ = endpoint
    bad = value(source("email", "records", query="Cassia"), source("email", "records", query=query))
    client.outputs = [bad, bad]
    events = asyncio.run(request("Read email from Cassia and email about Guide to Gardening to Taylor"))
    assert calls == [] and "clarify" in [e.get("intent_disposition") for e in events]


def test_actor_quoted_second_to_title_remains_exact_data(endpoint):
    request, client, calls, _, _ = endpoint
    query = "Guide to Gardening to Taylor"
    client.outputs = [value(source("email", "records", query="Cassia"), source("email", "records", query=query))]
    events = asyncio.run(request('Read email from Cassia and email about "' + query + '"'))
    assert calls == [("view_emails", {"query": "Cassia", "strict_match": True}),
                     ("view_emails", {"query": query, "strict_match": True})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("joiner", ["and", "plus", "and then"])
def test_actor_source_exclusion_does_not_cancel_later_positive_messages_read(endpoint, joiner):
    request, client, calls, _, _ = endpoint
    data = value(source("notes", "records", query="cedar maps"), source("messages", "records"))
    data["excluded_sources"] = ["email"]
    client.outputs = [data]
    events = asyncio.run(request("Find notes about cedar maps without email " + joiner + " read messages"))
    assert calls == [("search_notes", {"query": "cedar maps"}), ("view_messages", {})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


def test_actor_quoted_polite_share_request_stays_literal(endpoint):
    request, client, calls, _, _ = endpoint
    query = "cedar maps and would you mind sharing it with Mom"
    client.outputs = [value(source("notes", "records", query=query))]
    events = asyncio.run(request('Find notes about "' + query + '"'))
    assert calls == [("search_notes", {"query": query})]
    assert "compiled" in [e.get("intent_disposition") for e in events]


@pytest.mark.parametrize("joiner,tail", LATER_EFFECT_CASES)
@pytest.mark.parametrize("negative", ["do not delete it", "please never share it with Mom"])
def test_actor_later_effect_after_negation_never_succeeds_as_notes_only(endpoint, monkeypatch, joiner, tail, negative):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    prompt = "Find notes about amber route and " + negative + " " + joiner + " " + tail
    client.outputs = [value(source("notes", "records", query="amber route"))] * 2
    async def fake_agent(_client, _model, _messages, emit, _approver, **kwargs):
        await emit({"type": "text", "text": "Synthetic guarded action needs its own handling."})
        return "Synthetic guarded action needs its own handling."
    monkeypatch.setattr(main, "run_agent", fake_agent)
    events = asyncio.run(request(prompt, session_id=sid))
    assert not calls and not client.calls
    assert "compiled" not in [event.get("intent_disposition") for event in events]
    assert "Synthetic note: amber route" not in text(events)
    if "reminders" in tail:
        action = tail.removeprefix("please ").split()[0]
        clarification = {
            "update": "Which reminder should I update?",
            "mark": "I couldn’t find an active reminder matching “reminders.” Which reminder should I mark done?",
            "clear": "Which reminders should I delete: today, tomorrow, past due, upcoming, or all?",
        }[action]
        assert text(events) in {"Synthetic guarded action needs its own handling.", clarification}
        workflow = store.latest_workflow(sid)
        assert workflow is None or workflow.get("kind") != "deliver_summary"


@pytest.mark.parametrize("joiner,tail", LATER_EFFECT_CASES)
def test_actor_quoted_effect_sequence_has_exact_response_and_no_workflow(endpoint, joiner, tail):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    query = "amber route and do not delete it " + joiner + " " + tail
    client.outputs = [value(source("notes", "records", query=query))]
    events = asyncio.run(request('Find notes about "' + query + '"', session_id=sid))
    assert calls == [("search_notes", {"query": query})]
    assert text(events) == "Synthetic note: " + query
    assert store.latest_workflow(sid) is None


@pytest.mark.parametrize("joiner", READ_AFTER_NEGATIVE_JOINERS)
@pytest.mark.parametrize("omitted", [False, True])
def test_actor_read_after_negation_preserves_requested_source_coverage(endpoint, joiner, omitted):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    prompt = "Find notes about amber route and do not share it with Mom " + joiner + " read my reminders"
    data = value(source("notes", "records", query="amber route"))
    if not omitted:
        data["sources"].append(source("reminders", "records"))
    client.outputs = [data, data]
    events = asyncio.run(request(prompt, session_id=sid))
    assert store.latest_workflow(sid) is None
    if omitted:
        assert not calls and "clarify" in [event.get("intent_disposition") for event in events]
    else:
        assert calls == [("search_notes", {"query": "amber route"}), ("search_reminders", {"query": "", "scope": "all"})]
        assert "Synthetic note: amber route" in text(events) and "Synthetic exact fixture records." in text(events)
        assert "compiled" in [event.get("intent_disposition") for event in events]


@pytest.mark.parametrize("query", ["I would prefer not to send it to Mom", "amber route and do not share it with Mom and afterwards read my reminders"])
def test_actor_quoted_preference_and_later_read_words_remain_literal_data(endpoint, query):
    request, client, calls, store, _ = endpoint
    sid = store.create_session()
    client.outputs = [value(source("notes", "records", query=query))]
    events = asyncio.run(request('Find notes about "' + query + '"', session_id=sid))
    assert calls == [("search_notes", {"query": query})]
    assert text(events) == "Synthetic note: " + query
    assert store.latest_workflow(sid) is None
