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
from tests.test_router_intent_core import FakeClient, CONFIG, MODEL, TARGET, source, value


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
