"""Synthetic-only structured intent/compiler/planner contracts, no live clients."""
from __future__ import annotations
import asyncio
import copy
import json
from datetime import datetime
from types import SimpleNamespace
import pytest
import httpx
from dataclasses import replace
from service.config.endpoints import Endpoint, Target
from service.router.intent import planner
import service.tools  # register signatures only
from service.router.intent import SCHEMA, compile_intent, validate_intent, InvalidIntent, UnsupportedRead, plan_read, resident_eligible
from service.router.intent.grammar import personal_agenda_period
from service.workflows.reads import compile_read

NOW = datetime(2026, 10, 5, 10, 0)
MODEL = "Ling-3.0-tiny-oQ6e"
TARGET = Target("router", Endpoint("local", "http://127.0.0.1:8000", "local_omlx", True), MODEL)


@pytest.fixture(autouse=True)
def configured_router_target(monkeypatch):
    monkeypatch.setattr(planner, "role_target", lambda role: TARGET if role == "router" else None)


CONFIG = {"enabled": True, "domains": ["calendar", "reminders", "email", "messages", "notes"]}


def value(*sources, **kw):
    return {"version": 1, "kind": "read", "sources": list(sources), "excluded_sources": [], "unsupported_constraints": [], **kw}


def source(domain, operation="overview", **kw):
    return {"domain": domain, "operation": operation, **kw}


class FakeClient:
    managed = True
    provider = SimpleNamespace(name="omlx")
    target = TARGET
    base_url = TARGET.endpoint.base_url
    endpoint_name = TARGET.endpoint.name
    api_prefix = TARGET.endpoint.api_prefix
    _credential_transport = SimpleNamespace(origin=httpx.URL(TARGET.endpoint.base_url), backend=object())

    def __init__(self, *outputs, loaded=True):
        self.outputs = list(outputs)
        self.loaded = loaded
        self.calls = []
        self.context = []

    async def status(self):
        self.calls.append("status")
        return {"models": [{"id": MODEL, "loaded": self.loaded}]}

    async def chat(self, model, messages, **kwargs):
        self.calls.append("chat")
        self.context.append(copy.deepcopy(messages))
        assert model == MODEL and kwargs["response_format"]["json_schema"]["schema"] == SCHEMA
        output = self.outputs.pop(0)
        return {"choices": [{"finish_reason": "stop", "message": {"content": output if isinstance(output, str) else json.dumps(output)}}]}

    async def ensure_only(self, *a, **k):
        raise AssertionError("Planner must never change residency")


def run(prompt, client, **kw):
    return asyncio.run(plan_read(prompt, client=client, model=MODEL, config=CONFIG, now=NOW, **kw))


@pytest.mark.parametrize("phrase", ["what is up for this week", "what's up this week?", "what do I have next week", "what is coming up for this month"])
def test_shared_personal_grammar(phrase):
    assert personal_agenda_period(phrase)
    assert compile_read(phrase)[0][0][0] == "get_upcoming"


@pytest.mark.parametrize("phrase", ["what is up in Berlin this week", "what public events are happening this week", "what is up with inflation this week", "what is up for this week and email Mom"])
def test_personal_grammar_never_strips_public_subject_or_action(phrase):
    assert personal_agenda_period(phrase) is None


def test_multisource_compile_preserves_every_source_and_typed_filters():
    data = value(source("email", "records", time={"named": "yesterday"}, unread=True), source("messages", time={"named": "yesterday"}))
    intent = validate_intent(data, "Read my unread email and recap texts from yesterday", now=NOW)
    assert compile_intent(intent, now=NOW)[0] == [("view_emails", {"unread": True, "period": "yesterday"}), ("summarize_messages", {"period": "yesterday"})]


def test_calendar_does_not_add_unrequested_reminders():
    intent = validate_intent(value(source("calendar", time={"named": "this week"})), "Recap my calendar this week", now=NOW)
    assert compile_intent(intent, now=NOW)[0] == [("get_upcoming", {"period": "this week", "calendar_only": True})]


def test_declared_matching_agenda_scopes_coalesce_without_source_loss():
    data = value(source("calendar", time={"named": "this week"}), source("reminders", time={"named": "this week"}))
    intent = validate_intent(data, "What is up for this week", now=NOW)
    assert compile_intent(intent, now=NOW)[0] == [("get_upcoming", {"period": "this week", "calendar_only": False})]


def test_same_tool_distinct_scopes_survive_and_identical_reads_dedupe():
    intent = validate_intent(value(source("notes", "records", query="Alpha"), source("notes", "records", query="Beta"), source("notes", "records", query="Alpha")), 'Find notes named "Alpha" and "Beta"', now=NOW)
    assert compile_intent(intent, now=NOW)[0] == [("search_notes", {"query": "Alpha"}), ("search_notes", {"query": "Beta"})]


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=True), lambda d: d.update(version=True),
    lambda d: d["sources"][0].update(unread="true"), lambda d: d["sources"][0].update(count=True),
    lambda d: d["sources"][0].update(query="is:unread"), lambda d: d["sources"][0].update(query="Youmani"),
    lambda d: d["sources"][0].update(time={"start": "2026-10-06"}),
    lambda d: d["sources"][0].update(time={"date": "2026-02-30"}),
    lambda d: d["sources"][0].update(time={"named": "last month"}),
    lambda d: d["sources"][0].update(count=20),
])
def test_invalid_shapes_invented_filters_and_bad_dates_rejected(mutate):
    data = value(source("email", "records", query="Imani"))
    mutate(data)
    with pytest.raises(InvalidIntent):
        validate_intent(data, "Read email from Imani", now=NOW)


@pytest.mark.parametrize("prompt,data", [
    ("Recap email and texts", value(source("email"))),
    ("Read unread email", value(source("email", "records"))),
    ('Find note named "Exact +title"', value(source("notes", "records", query="title"))),
    ("Read messages from +14155550123", value(source("messages", "records", query="14155550123"))),
    ("Read email except Acme", value(source("email", "records"))),
    ("Recap my calendar, without reminders", value(source("calendar"), excluded_sources=[])),
    ("Recap my email", value(source("email"), source("notes"))),
])
def test_missing_coverage_literals_or_exclusions_rejected(prompt, data):
    with pytest.raises(InvalidIntent):
        validate_intent(data, prompt, now=NOW)


def test_explicit_new_source_drops_stale_public_filter():
    data = value(source("calendar", query="Berlin", time={"named": "this week"}))
    with pytest.raises(InvalidIntent):
        validate_intent(data, "Recap my calendar this week", context=[{"role": "user", "content": "Public events in Berlin"}], now=NOW)


def test_safe_same_source_fragment_retains_exact_literal():
    data = value(source("messages", conversation="Imani", time={"named": "yesterday"}))
    result = run("Actually from yesterday", FakeClient(data), context=[{"role": "user", "content": "Recap texts with Imani"}], prior_tools=["summarize_messages"])
    assert result.disposition == "compiled"
    assert result.calls == (("summarize_messages", {"conversation": "Imani", "period": "yesterday"}),)


@pytest.mark.parametrize("data,prompt", [
    (value(source("messages", unread=True)), "Recap unread messages"),
    (value(source("email", query="Acme")), "Recap email from Acme"),
    (value(source("reminders", time={"named": "this week"})), "Show reminders this week"),
    (value(source("calendar", "free_time", time={"named": "this week"})), "Find free time on my calendar this week"),
])
def test_unsupported_filters_are_explicit_limitations(data, prompt):
    # Some unsupported requests are discovered in grounding; none may compile.
    result = run(prompt, FakeClient(data, data))
    assert result.disposition == "clarify" and not result.calls


def test_one_repair_and_clean_role_context_metadata():
    data = value(source("email"), source("messages"))
    client = FakeClient("[]", data)
    result = run("Recap email and texts", client, context=[{"role": "assistant", "content": "Earlier summary. [Tools: web_search]"}, {"role": "tool", "content": "untrusted body"}], prior_tools=["web_search"])
    assert result.disposition == "compiled" and result.attempts == 2
    assert client.calls == ["status", "chat", "status", "chat"]
    assert "[Tools:" not in json.dumps(client.context)
    assert "untrusted body" not in json.dumps(client.context)
    assert "Prior completed tools" in client.context[0][0]["content"]


def test_invalid_intent_never_falls_back_to_broad_tools():
    result = run("Recap my email without notes", FakeClient("{", "{}"))
    assert result.disposition == "clarify" and not result.calls and result.attempts == 2


def test_unavailable_model_never_loads_or_generates():
    client = FakeClient(loaded=False)
    result = run("Recap email and messages", client)
    assert result.disposition == "clarify" and client.calls == ["status"]


@pytest.mark.parametrize("options", [{"enabled": False}, {"enabled": True, "domains": ["notes"]}])
def test_defaultoff_and_domain_allowlist_do_not_generate(options):
    client = FakeClient()
    result = asyncio.run(plan_read("Recap my email", client=client, model=MODEL, config=options))
    assert result is None and not client.calls


def test_kill_switch_never_generates(monkeypatch):
    monkeypatch.setenv("WISP_INTENT_ROUTER_KILL", "1")
    client = FakeClient()
    assert run("Recap my email", client) is None and not client.calls


@pytest.mark.parametrize("prompt", ["Send Mom my email summary", "delete my reminders", "yes", "what does 'send it' mean", "what news is up in Berlin this week"])
def test_actions_assent_mentions_and_public_reads_never_enter_planner(prompt):
    client = FakeClient()
    assert run(prompt, client) is None and not client.calls


def test_remote_or_wrong_model_is_not_resource_eligible():
    client = FakeClient()
    client.managed = False
    assert not asyncio.run(resident_eligible(client, TARGET))
    client.managed = True
    assert not asyncio.run(resident_eligible(client, replace(TARGET, model="OtherModel")))
    client.target = SimpleNamespace(model="OtherModel", endpoint=SimpleNamespace(managed=True))
    assert not asyncio.run(resident_eligible(client, TARGET))
    assert not client.calls


def test_deadline_cancels_generation_without_retry():
    class Slow(FakeClient):
        async def chat(self, *a, **k):
            self.calls.append("chat")
            try:
                await asyncio.sleep(10)
            finally:
                self.calls.append("cancelled")
    client = Slow()
    result = asyncio.run(plan_read("Recap my email", client=client, model=MODEL, config={**CONFIG, "deadline_seconds": .05}))
    assert result.disposition == "clarify" and client.calls == ["status", "chat", "cancelled"]


def test_outer_cancellation_is_not_reclassified_as_clarification():
    class Block(FakeClient):
        async def status(self):
            await asyncio.sleep(10)
    async def cancel():
        task = asyncio.create_task(plan_read("Recap my email", client=Block(), model=MODEL, config=CONFIG))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())


def test_loop_merge_keeps_scope_and_failure_evidence():
    from service.agent.loop import _merge_results
    result = _merge_results([("search_notes", "Alpha note"), ("search_notes", "Beta note"), ("get_upcoming", "Work calendar could not be checked")])
    assert "Alpha note" in result and "Beta note" in result and "could not be checked" in result
    assert _merge_results([("summarize_messages", "Exact digest")]) == "Exact digest"


def test_early_read_does_not_swallow_compound_filtered_digest():
    assert compile_read("Show my email summary from Acme and my messages") is None
    assert compile_read("Show my email summary without notes") == ([("summarize_emails", {})], "")


@pytest.mark.parametrize("time,prompt", [
    ({"month": "2026-10"}, "Recap email for October 2026"),
    ({"date": "2026-10-09"}, "Recap my calendar on Friday"),
    ({"date": "2026-10-16"}, "Recap my calendar next Friday"),
    ({"start": "2026-10-05", "end": "2026-10-06"}, "Recap email today and tomorrow"),
])
def test_natural_dates_canonicalize_against_local_frozen_clock(time, prompt):
    domain = "email" if "email" in prompt else "calendar"
    intent = validate_intent(value(source(domain, time=time)), prompt, now=NOW)
    assert compile_intent(intent, now=NOW)[0]


def test_same_source_explicit_correction_keeps_sender_but_source_change_does_not():
    context = [{"role": "user", "content": "Read email from Acme"}]
    data = value(source("email", "records", query="Acme", unread=True))
    assert run("Now only unread email", FakeClient(data), context=context).disposition == "compiled"
    changed = value(source("notes", "records", query="Acme"))
    assert run("Now only notes", FakeClient(changed, changed), context=context).disposition == "clarify"
    missing = value(source("email", "records", unread=True))
    assert run("Now only unread email", FakeClient(missing, missing), context=context).disposition == "clarify"


def test_explicit_corrected_sender_replaces_adjacent_filter():
    data = value(source("email", "records", query="Imani"))
    result = run("Actually email from Imani", FakeClient(data), context=[{"role": "user", "content": "Read email from Acme"}])
    assert result.disposition == "compiled" and result.calls[0][1]["query"] == "Imani"


def test_direct_router_weekly_fast_path_and_public_scope_remain_distinct():
    from service.router.router import route
    result = asyncio.run(route("What is up for this week", last_user="public events in Berlin", last_tools="web_search"))
    assert result.direct_calls == [("get_upcoming", {"period": "this week"})]
    public = asyncio.run(route("What is up in Berlin this week"))
    assert "get_upcoming" not in (public.tool_subset or [])


def test_contextual_source_authority_rejects_wrong_domain_even_with_no_current_source():
    bad = value(source("calendar", time={"named": "tomorrow"}))
    result = run("Same for tomorrow", FakeClient(bad, bad), context=[{"role": "user", "content": "Recap email"}], prior_tools=["summarize_emails"])
    assert result.disposition == "clarify" and not result.calls
    correct = value(source("email", time={"named": "tomorrow"}))
    assert run("Same for tomorrow", FakeClient(correct), context=[{"role": "user", "content": "Recap email"}], prior_tools=["summarize_emails"]).disposition == "compiled"


@pytest.mark.parametrize("data,prompt", [
    (value(source("email", unread=True, time={"named": "yesterday"})), "Recap unread email yesterday"),
    (value(source("email", count=5, time={"named": "yesterday"})), "Recap five emails yesterday"),
    (value(source("email", count=5, unread=True)), "Recap five unread emails"),
    (value(source("messages", count=5, time={"named": "yesterday"})), "Recap five messages yesterday"),
    (value(source("calendar", time={"named": "last week"})), "Recap my calendar last week"),
    (value(source("calendar", "free_time", time={"rolling_days": 30})), "Find free time on my calendar for the next 30 days"),
])
def test_registered_but_semantically_unsupported_combinations_never_compile(data, prompt):
    result = run(prompt, FakeClient(data, data))
    assert result.disposition == "clarify" and not result.calls


def test_weekend_scope_is_resolved_to_inclusive_dates_locally():
    intent = validate_intent(value(source("calendar", time={"named": "this weekend"})), "Recap my calendar this weekend", now=NOW)
    assert compile_intent(intent, now=NOW)[0] == [("get_upcoming", {"period": "2026-10-10 to 2026-10-11", "calendar_only": True})]


def test_scope_labels_escape_literal_markdown_and_preserve_failed_receipt():
    from service.workflows.reads import merge_read_results
    calls = [{"id": "a", "name": "search_notes", "args": {"query": "[link](evil)"}}, {"id": "b", "name": "search_notes", "args": {"query": "Beta"}}]
    results = [{"id": "a", "name": "search_notes", "result": "No match in available notes", "status": "no_match"}, {"id": "b", "name": "search_notes", "result": "Beta note", "status": "succeeded"}]
    text = merge_read_results(calls, results)
    assert r"\[link\]\(evil\)" in text and "No match" in text and "Beta note" in text


@pytest.mark.parametrize("raw,prompt", [
    (source("email", "records"), "Read five emails"),
    (source("email", "records", count=5), "Read email for October 5"),
    (source("reminders", scope="overdue"), "Read my reminders"),
])
def test_counts_and_reminder_scopes_cannot_be_omitted_or_invented(raw, prompt):
    result = run(prompt, FakeClient(value(raw), value(raw)))
    assert result.disposition == "clarify" and not result.calls


@pytest.mark.parametrize("raw,prompt,expected", [
    (source("reminders", time={"named": "today"}), "Read reminders today", "today"),
    (source("reminders", scope="tomorrow"), "Read reminders tomorrow", "tomorrow"),
    (source("reminders", scope="overdue"), "Read overdue reminders", "past_due"),
])
def test_supported_reminder_scopes_match_registered_semantics(raw, prompt, expected):
    result = run(prompt, FakeClient(value(raw)))
    assert result.disposition == "compiled"
    assert result.calls == (("search_reminders", {"query": "", "scope": expected}),)


def test_wire_intent_disposition_preserves_existing_diagnostics_vocabulary(tmp_path):
    from service.router.router import RouteDecision
    from service.diagnostics import Trace
    route = RouteDecision("agent", MODEL, False, "intent", "private reason", route_source="intent_clarify")
    event = {"type": "routed", **route.as_dict()}
    assert event["route_source"] == "model" and event["intent_disposition"] == "clarify"
    trace = Trace("agent", root=tmp_path)
    trace.observe(event)
    assert trace.events[-1]["route_source"] == "model"
    assert "intent_disposition" not in trace.events[-1] and "private" not in json.dumps(trace.events)


def test_schema_asset_matches_callable_contract():
    from pathlib import Path
    import service.router.intent as package
    schema = Path(package.__file__).parent / "intent.schema.v1.json"
    assert json.loads(schema.read_text()) == SCHEMA


def test_missing_named_search_filter_cannot_become_unfiltered_notes():
    result = run("Find my bike lock code in notes", FakeClient(value(source("notes", "records")), value(source("notes", "records"))))
    assert result.disposition == "clarify" and not result.calls


def test_registered_strict_message_capability_is_used_exactly(monkeypatch):
    from dataclasses import replace
    from service.tools.registry import REGISTRY
    tool = REGISTRY["view_messages"]
    parameters = copy.deepcopy(tool.parameters)
    parameters["properties"]["strict_match"] = {"type": "boolean"}
    monkeypatch.setitem(REGISTRY, "view_messages", replace(tool, parameters=parameters))
    data = value(source("messages", "records", query="+14155550123"))
    result = run("Read messages from +14155550123", FakeClient(data))
    assert result.disposition == "compiled"
    assert result.calls == (("view_messages", {"strict_match": True, "query": "+14155550123"}),)


def test_loop_only_deduplicates_receipts_with_proven_equal_scope():
    from service.agent.loop import _merge_results
    receipts = [("search_notes", "Exact note", {"query": "Alpha"}), ("search_notes", "Exact note", {"query": "Beta"}), ("search_notes", "Exact note", {"query": "Alpha"})]
    text = _merge_results(receipts)
    assert text.count("Exact note") == 2 and "Alpha" in text and "Beta" in text
    assert _merge_results([("search_notes", "Same"), ("search_notes", "Same")]).count("Same") == 2


def test_model_noaction_output_does_not_drop_explicit_requested_read():
    noaction = value(kind="none")
    result = run("Recap my email", FakeClient(noaction, noaction))
    assert result.disposition == "clarify" and not result.calls


@pytest.mark.parametrize("prompt,options", [
    ("Recap my email", {"enabled": False}),
    ("Recap my email", {"enabled": True, "domains": ["notes"]}),
    ("hello", CONFIG),
])
def test_ineligible_request_never_resolves_target(monkeypatch, prompt, options):
    def forbidden(role):
        raise AssertionError("ineligible request must not inspect endpoint configuration")
    monkeypatch.setattr(planner, "role_target", forbidden)
    client = FakeClient()
    assert asyncio.run(plan_read(prompt, client=client, config=options)) is None
    assert not client.calls


@pytest.mark.parametrize("mutation", [
    lambda c: setattr(c, "base_url", "https://remote.example"),
    lambda c: setattr(c, "base_url", None),
    lambda c: setattr(c, "endpoint_name", "other"),
    lambda c: setattr(c, "provider", None),
    lambda c: setattr(c, "provider", SimpleNamespace(name="openai-compatible")),
    lambda c: setattr(c, "api_prefix", "/v2"),
    lambda c: setattr(c, "target", replace(TARGET, role="fast")),
    lambda c: setattr(c, "target", replace(TARGET, model="Ling-other")),
    lambda c: setattr(c, "target", replace(TARGET, revision="different")),
    lambda c: setattr(c, "target", replace(TARGET, context_window=4096)),
    lambda c: setattr(c, "target", replace(TARGET, endpoint=replace(TARGET.endpoint, credential_ref="env:OTHER"))),
    lambda c: setattr(c, "_credential_transport", None),
    lambda c: setattr(c, "_credential_transport", SimpleNamespace(origin=httpx.URL("http://127.0.0.1:9000"), backend=object())),
    lambda c: setattr(c, "_credential_transport", SimpleNamespace(origin=httpx.URL(TARGET.endpoint.base_url), backend=None)),
])
def test_mismatched_identity_fails_before_status_even_with_injected_eligibility(mutation):
    client = FakeClient()
    mutation(client)
    async def forbidden(*args):
        raise AssertionError("identity must precede resource seam")
    result = run("Recap my email", client, eligibility=forbidden)
    assert result.disposition == "clarify" and not result.calls and result.attempts == 0
    assert not client.calls


@pytest.mark.parametrize("target", [None, replace(TARGET, role="fast"),
    replace(TARGET, model="OtherModel"),
    replace(TARGET, endpoint=replace(TARGET.endpoint, managed=False)),
    replace(TARGET, endpoint=replace(TARGET.endpoint, base_url="https://remote.example")),
    replace(TARGET, endpoint=replace(TARGET.endpoint, provider="openai-compatible")),
])
def test_invalid_configured_router_target_never_contacts_client(monkeypatch, target):
    monkeypatch.setattr(planner, "role_target", lambda role: target)
    client = FakeClient()
    result = run("Recap my email", client)
    assert result.disposition == "clarify" and not client.calls


def test_missing_binding_or_resolution_failure_fails_closed(monkeypatch):
    def missing(role):
        raise ValueError("missing registered endpoint")
    monkeypatch.setattr(planner, "role_target", missing)
    client = FakeClient()
    result = run("Recap my email", client)
    assert result.disposition == "clarify" and not client.calls


def test_configured_target_is_authoritative_over_model_argument_and_config(monkeypatch):
    client = FakeClient(value(source("email")))
    result = asyncio.run(plan_read("Recap my email", client=client, model="Ling-other", config=CONFIG))
    assert result.disposition == "clarify" and not client.calls
    roles = []
    def configured(role):
        roles.append(role)
        return TARGET
    monkeypatch.setattr(planner, "role_target", configured)
    result = asyncio.run(plan_read("Recap my email", client=client, config={**CONFIG, "model": "remote-override"}))
    assert result.disposition == "compiled" and roles == ["router"]
    assert client.calls == ["status", "chat"]


def test_legacy_client_preserves_canonical_transport_and_no_secret_access():
    client = FakeClient(value(source("email")))
    client.target = None
    client.base_url += "/"
    class MetadataOnly:
        origin = httpx.URL(TARGET.endpoint.base_url)
        backend = object()
        @property
        def key(self):
            raise AssertionError("never inspect secrets")
        @property
        def key_loader(self):
            raise AssertionError("never resolve credentials")
    client._credential_transport = MetadataOnly()
    assert run("Recap my email", client).disposition == "compiled"
    assert not asyncio.run(resident_eligible(client, replace(TARGET, revision="r1")))
    assert not asyncio.run(resident_eligible(client, replace(TARGET, endpoint=replace(TARGET.endpoint, credential_ref="env:OTHER"))))
    assert client.calls == ["status", "chat"]


@pytest.mark.parametrize("prompt,expected", [
    ("what is up tomorrow?", {"period": "tomorrow", "calendar_only": True}),
    ("what is up today?", {"period": "today", "calendar_only": True}),
    ("Show my calendar this week", {"period": "this week", "calendar_only": True}),
    ("Check my calendar tomorrow", {"period": "tomorrow", "calendar_only": True}),
    ("List calendar next month", {"period": "next month", "calendar_only": True}),
    ("Show my agenda tomorrow", {"period": "tomorrow"}),
    ("Show my schedule this week", {"period": "this week"}),
    ("What is up for this week", {"period": "this week"}),
    ("Show my calendar this weekend", {"period": "2026-10-10 to 2026-10-11", "calendar_only": True}),
])
def test_shared_shortcut_preserves_source_scope_and_frozen_dates(prompt, expected):
    from service.router.intent.grammar import personal_agenda_args
    assert personal_agenda_args(prompt, now=NOW) == expected
    assert compile_read(prompt, now=NOW) == ([("get_upcoming", expected)], "")
    from service.router import router
    decision = asyncio.run(router.route(prompt, intent_now=NOW))
    assert decision.direct_calls == [("get_upcoming", expected)]
    assert decision.verified_results_only


@pytest.mark.parametrize("prompt", [
    "Show my calendar this week and reminders", "What is up tomorrow in Berlin?",
    "Show my calendar tomorrow and send it to Mom", "Show my calendar tomorrow without Work",
])
def test_shared_source_shortcut_does_not_drop_second_source_public_subject_action_or_filter(prompt):
    from service.router.intent.grammar import personal_agenda_args
    assert personal_agenda_args(prompt, now=NOW) is None
    assert compile_read(prompt, now=NOW) is None


@pytest.mark.parametrize("prompt,bad", [
    ("Read email for 2026-10-01", source("email", "records")),
    ("Read email for 2026-10-01", source("email", "records", time={"date": "2026-10-02"})),
    ("Read email for October 2026", source("email", "records")),
    ("Read email for October 2026", source("email", "records", time={"month": "2026-11"})),
    ("Read email for October 12", source("email", "records")),
    ("Read email on Tuesday", source("email", "records")),
    ("Read overdue reminders", source("reminders")),
    ("Read overdue reminders", source("reminders", scope="all")),
    ("Read upcoming reminders", source("reminders", scope="all")),
    ("Find a 90 minute free slot on my calendar tomorrow", source("calendar", "free_time", time={"named": "tomorrow"})),
    ("Find a 90 minute free slot on my calendar tomorrow", source("calendar", "free_time", time={"named": "tomorrow"}, minutes=30)),
    ("Find a 90 minute free slot on my calendar tomorrow", source("calendar", time={"named": "tomorrow"}, query="free slot")),
    ("Find notes about audit blueprints", source("notes", "records", query="audit")),
    ("Read email from Acme research team", source("email", "records", query="Acme")),
    ("Read email before 2026-10-01", source("email", "records", time={"date": "2026-10-01"})),
    ("Read email tomorrow at 3pm", source("email", "records", time={"named": "tomorrow"})),
])
def test_requested_constraints_cannot_be_omitted_or_contradicted(prompt, bad):
    result = run(prompt, FakeClient(value(bad), value(bad)))
    assert result.disposition == "clarify" and not result.calls


@pytest.mark.parametrize("prompt,good,expected", [
    ("Read email for 2026-10-01", source("email", "records", time={"date": "2026-10-01"}), ("view_emails", {"period": "2026-10-01"})),
    ("Read email for October 2026", source("email", "records", time={"month": "2026-10"}), ("view_emails", {"period": "2026-10"})),
    ("Read email for October 12", source("email", "records", time={"date": "2026-10-12"}), ("view_emails", {"period": "2026-10-12"})),
    ("Read email on Tuesday", source("email", "records", time={"date": "2026-10-06"}), ("view_emails", {"period": "2026-10-06"})),
    ("Read overdue reminders", source("reminders", scope="overdue"), ("search_reminders", {"query": "", "scope": "past_due"})),
    ("Read upcoming reminders", source("reminders", scope="upcoming"), ("search_reminders", {"query": "", "scope": "upcoming"})),
    ("Find a 90 minute free slot on my calendar tomorrow", source("calendar", "free_time", time={"named": "tomorrow"}, minutes=90), ("find_free_time", {"period": "tomorrow", "minutes": 90})),
    ("Find a 1.5 hour free slot on my calendar tomorrow", source("calendar", "free_time", time={"named": "tomorrow"}, minutes=90), ("find_free_time", {"period": "tomorrow", "minutes": 90})),
    ("Find an hour and 30 minute free slot on my calendar tomorrow", source("calendar", "free_time", time={"named": "tomorrow"}, minutes=90), ("find_free_time", {"period": "tomorrow", "minutes": 90})),
    ("Find notes about audit blueprints", source("notes", "records", query="audit blueprints"), ("search_notes", {"query": "audit blueprints"})),
    ("Find notes about audit blueprints from yesterday", source("notes", "records", query="audit blueprints", time={"named": "yesterday"}), ("search_notes", {"query": "audit blueprints", "period": "yesterday"})),
    ("Find notes about 2026-10-01", source("notes", "records", query="2026-10-01"), ("search_notes", {"query": "2026-10-01"})),
    ("Read email from Acme research team", source("email", "records", query="Acme research team"), ("view_emails", {"query": "Acme research team", "strict_match": True})),
])
def test_supported_requested_constraints_compile_without_default_substitution(prompt, good, expected):
    result = run(prompt, FakeClient(value(good)))
    assert result.disposition == "compiled" and result.calls == (expected,)


def test_source_owned_dates_cannot_swap_or_disappear_and_shared_date_is_retained():
    prompt = "Read email for 2026-10-01 and notes for 2026-10-02"
    good = value(source("email", "records", time={"date": "2026-10-01"}), source("notes", "records", time={"date": "2026-10-02"}))
    assert run(prompt, FakeClient(good)).disposition == "compiled"
    for bad in [value(source("email", "records", time={"date": "2026-10-02"}), source("notes", "records", time={"date": "2026-10-01"})),
                value(source("email", "records", time={"date": "2026-10-01"}), source("notes", "records"))]:
        result = run(prompt, FakeClient(bad, bad))
        assert result.disposition == "clarify" and not result.calls
    shared = value(source("email", "records", time={"date": "2026-10-01"}), source("notes", "records", time={"date": "2026-10-01"}))
    assert run("Read email and notes for 2026-10-01", FakeClient(shared)).disposition == "compiled"


def test_corrections_replace_current_date_scope_duration_and_unquoted_query():
    result = run("Same for tomorrow", FakeClient(value(source("email", "records", time={"named": "tomorrow"}))),
                 context=[{"role": "user", "content": "Read email for yesterday"}], prior_tools=["view_emails"])
    assert result.disposition == "compiled"
    result = run("Only overdue", FakeClient(value(source("reminders", scope="overdue"))),
                 context=[{"role": "user", "content": "Read all reminders"}], prior_tools=["search_reminders"])
    assert result.disposition == "compiled"
    result = run("Now find notes about revised blueprints", FakeClient(value(source("notes", "records", query="revised blueprints"))),
                 context=[{"role": "user", "content": "Find notes about audit blueprints"}], prior_tools=["search_notes"])
    assert result.disposition == "compiled"


def test_complete_query_does_not_authorize_extra_truncated_same_source_read():
    bad = value(source("notes", "records", query="audit blueprints"), source("notes", "records", query="audit"))
    result = run("Find notes about audit blueprints", FakeClient(bad, bad))
    assert result.disposition == "clarify" and not result.calls


@pytest.mark.parametrize("prompt,data,expected", [
    ("Read email today plus messages yesterday", value(source("email", "records", time={"named": "today"}), source("messages", "records", time={"named": "yesterday"})),
     (("view_emails", {"period": "today"}), ("view_messages", {"period": "yesterday"}))),
    ("Find notes about audit blueprints from yesterday limit to five", value(source("notes", "records", query="audit blueprints", time={"named": "yesterday"}, count=5)),
     (("search_notes", {"query": "audit blueprints", "count": 5, "period": "yesterday"}),)),
    ("Find notes about audit blueprints at most five", value(source("notes", "records", query="audit blueprints", count=5)),
     (("search_notes", {"query": "audit blueprints", "count": 5}),)),
])
def test_positive_independent_dates_and_complete_query_date_limit_suffixes(prompt, data, expected):
    result = run(prompt, FakeClient(data))
    assert result.disposition == "compiled" and result.calls == expected


@pytest.mark.parametrize("prompt,data", [
    ("Read email today plus messages yesterday", value(source("email", "records", time={"named": "yesterday"}), source("messages", "records", time={"named": "today"}))),
    ("Find notes about audit blueprints from yesterday limit to five", value(source("notes", "records", query="audit", time={"named": "yesterday"}, count=5))),
    ('Find notes named "audit blueprints"', value(source("notes", "records", query="audit blueprints"), source("notes", "records", query="audit"))),
    ('Read email from "Acme"', value(source("email", "records", query="Acme"), source("email", "records"))),
])
def test_requested_source_filters_do_not_authorize_extra_or_changed_reads(prompt, data):
    result = run(prompt, FakeClient(data, data))
    assert result.disposition == "clarify" and not result.calls


@pytest.mark.parametrize("word", ["overdue", "upcoming", "all"])
def test_reminder_query_literal_is_not_a_scope_instruction(word):
    result = run("Find reminders named " + word, FakeClient(value(source("reminders", "records", query=word))))
    assert result.calls == (("search_reminders", {"query": word, "scope": "all"}),)


def test_query_duration_literal_does_not_authorize_changed_calendar_operation():
    bad = value(source("calendar", "free_time", minutes=90), source("notes", "records", query="90 minute appointments"))
    result = run("Recap my calendar and find notes about 90 minute appointments", FakeClient(bad, bad))
    assert result.disposition == "clarify" and not result.calls


REPEATED_READ_PROMPT = "Read email from Elara for 2026-10-03; read email from Tobias for 2026-10-04"


def repeated_email(*, reverse=False, swapped=False):
    data = [source("email", "records", query="Elara", time={"date": "2026-10-04" if swapped else "2026-10-03"}),
            source("email", "records", query="Tobias", time={"date": "2026-10-03" if swapped else "2026-10-04"})]
    return value(*(reversed(data) if reverse else data))


def test_whole_occurrence_date_binding_rejects_swap_and_missing_clause():
    for bad in [repeated_email(swapped=True), value(repeated_email()["sources"][0])]:
        with pytest.raises(InvalidIntent):
            validate_intent(bad, REPEATED_READ_PROMPT, now=NOW)
        result = run(REPEATED_READ_PROMPT, FakeClient(bad, bad))
        assert result.disposition == "clarify" and not result.calls


@pytest.mark.parametrize("reverse", [False, True])
def test_whole_occurrence_matching_is_independent_of_model_entry_order(reverse):
    data = repeated_email(reverse=reverse)
    intent = validate_intent(data, REPEATED_READ_PROMPT, now=NOW)
    assert [(s.query, s.time.date) for s in intent.sources] == [(s["query"], s["time"]["date"]) for s in data["sources"]]
    assert run(REPEATED_READ_PROMPT, FakeClient(data)).disposition == "compiled"


@pytest.mark.parametrize("mutation", ["date", "account", "count", "operation", "unread", "query", "missing"])
def test_repeated_read_tuple_cannot_mix_individually_authorized_fields(mutation):
    prompt = ('Read two unread emails from Elara in account "Work" for 2026-10-03; '
              'read three emails from Tobias in account "Personal" for 2026-10-04')
    good = value(source("email", "records", query="Elara", account="Work", count=2, unread=True, time={"date": "2026-10-03"}),
                 source("email", "records", query="Tobias", account="Personal", count=3, time={"date": "2026-10-04"}))
    assert validate_intent(good, prompt, now=NOW)
    assert run(prompt, FakeClient(good)).disposition == "compiled"
    bad = copy.deepcopy(good)
    if mutation == "missing":
        bad["sources"].pop()
    elif mutation == "operation":
        bad["sources"][0]["operation"] = "overview"
    elif mutation == "unread":
        bad["sources"][0].pop("unread")
        bad["sources"][1]["unread"] = True
    else:
        key = "time" if mutation == "date" else mutation
        bad["sources"][0][key], bad["sources"][1][key] = bad["sources"][1][key], bad["sources"][0][key]
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)
    assert run(prompt, FakeClient(bad, bad)).disposition == "clarify"


@pytest.mark.parametrize("prompt,data", [
    ("Read email from Elara and email from Tobias for yesterday", value(source("email", "records", query="Elara", time={"named": "yesterday"}), source("email", "records", query="Tobias", time={"named": "yesterday"}))),
    ("Read email from Elara; read email from Tobias for tomorrow", value(source("email", "records", query="Elara"), source("email", "records", query="Tobias", time={"named": "tomorrow"}))),
    ("Find notes named Alpha; find notes named Beta", value(source("notes", "records", query="Alpha"), source("notes", "records", query="Beta"))),
    ("Recap email yesterday; read email today", value(source("email", "overview", time={"named": "yesterday"}), source("email", "records", time={"named": "today"}))),
])
def test_repeated_read_positive_shared_independent_and_operation_controls(prompt, data):
    assert validate_intent(data, prompt, now=NOW)
    assert run(prompt, FakeClient(data)).disposition == "compiled"


@pytest.mark.parametrize("fragment", ["Actually about birch diagrams", "Instead named birch diagrams", 'Actually about "birch diagrams"'])
def test_source_free_query_replacement_uses_new_literal_and_keeps_other_constraints(fragment):
    prior = [{"role": "user", "content": "Find five notes about cedar manuscripts from yesterday"}]
    good = value(source("notes", "records", query="birch diagrams", count=5, time={"named": "yesterday"}))
    bad = value(source("notes", "records", query="cedar manuscripts", count=5, time={"named": "yesterday"}))
    assert validate_intent(good, fragment, context=prior, prior_tools=["search_notes"], now=NOW)
    assert run(fragment, FakeClient(good), context=prior, prior_tools=["search_notes"]).disposition == "compiled"
    with pytest.raises(InvalidIntent):
        validate_intent(bad, fragment, context=prior, prior_tools=["search_notes"], now=NOW)
    assert run(fragment, FakeClient(bad, bad), context=prior, prior_tools=["search_notes"]).disposition == "clarify"


def test_source_free_query_replacement_with_multiple_contextual_sources_clarifies():
    prior = [{"role": "user", "content": "Find notes about cedar manuscripts and read email from Elara"}]
    data = value(source("notes", "records", query="birch diagrams"), source("email", "records", query="Elara"))
    with pytest.raises(InvalidIntent):
        validate_intent(data, "Actually about birch diagrams", context=prior, prior_tools=["search_notes", "view_emails"], now=NOW)
    assert run("Actually about birch diagrams", FakeClient(data, data), context=prior, prior_tools=["search_notes", "view_emails"]).disposition == "clarify"



def test_new_read_verb_keeps_trailing_date_on_its_own_occurrence():
    prompt = "Read email from Elara and read email from Tobias for yesterday"
    good = value(source("email", "records", query="Elara"), source("email", "records", query="Tobias", time={"named": "yesterday"}))
    bad = value(source("email", "records", query="Elara", time={"named": "yesterday"}), source("email", "records", query="Tobias", time={"named": "yesterday"}))
    assert validate_intent(good, prompt, now=NOW)
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)


def test_contextual_query_replacement_preserves_independently_requested_account():
    prior = [{"role": "user", "content": 'Read two unread emails from Elara in account "Work" for yesterday'}]
    good = value(source("email", "records", query="Tobias", account="Work", count=2, unread=True, time={"named": "yesterday"}))
    assert validate_intent(good, "Actually from Tobias", context=prior, prior_tools=["view_emails"], now=NOW)
    assert run("Actually from Tobias", FakeClient(good), context=prior, prior_tools=["view_emails"]).disposition == "compiled"
    bad = copy.deepcopy(good)
    bad["sources"][0]["query"] = "Elara"
    with pytest.raises(InvalidIntent):
        validate_intent(bad, "Actually from Tobias", context=prior, prior_tools=["view_emails"], now=NOW)



@pytest.mark.parametrize("fragment", ["Same for tomorrow", "Now only unread email"])
def test_contextual_repeated_reads_cannot_retain_superseded_date_or_unread(fragment):
    context = [{"role": "user", "content": REPEATED_READ_PROMPT}]
    data = repeated_email()
    stale = copy.deepcopy(data)
    for item in data["sources"]:
        if "tomorrow" in fragment:
            item["time"] = {"named": "tomorrow"}
        else:
            item["unread"] = True
    assert validate_intent(data, fragment, context=context, prior_tools=["view_emails"], now=NOW)
    assert run(fragment, FakeClient(data), context=context, prior_tools=["view_emails"]).disposition == "compiled"
    with pytest.raises(InvalidIntent):
        validate_intent(stale, fragment, context=context, prior_tools=["view_emails"], now=NOW)


def test_ambiguous_replacement_of_multiple_same_domain_reads_clarifies():
    context = [{"role": "user", "content": "Find notes named Alpha; find notes named Beta"}]
    data = value(source("notes", "records", query="birch diagrams"))
    with pytest.raises(InvalidIntent):
        validate_intent(data, "Actually about birch diagrams", context=context, prior_tools=["search_notes"], now=NOW)



REFERENCE_READ_CASES = [
    ("What appointments are on my calendar tomorrow? Question 1.", source("calendar", time={"named": "tomorrow"}), ("get_upcoming", {"period": "tomorrow", "calendar_only": True})),
    ("Read appointments on my calendar tomorrow", source("calendar", "records", time={"named": "tomorrow"}), ("get_upcoming", {"period": "tomorrow", "calendar_only": True})),
    ("Show appointments that are in the calendar tomorrow", source("calendar", "records", time={"named": "tomorrow"}), ("get_upcoming", {"period": "tomorrow", "calendar_only": True})),
    ("Read email in my inbox from yesterday", source("email", "records", time={"named": "yesterday"}), ("view_emails", {"period": "yesterday"})),
    ("Recap emails that are in my inbox", source("email"), ("summarize_emails", {})),
    ("Recap texts in my messages", source("messages"), ("summarize_messages", {})),
]


@pytest.mark.parametrize("prompt,good,expected", REFERENCE_READ_CASES)
def test_referential_source_aliases_are_one_requested_read(prompt, good, expected):
    assert validate_intent(value(good), prompt, now=NOW)
    result = run(prompt, FakeClient(value(good)))
    assert result.disposition == "compiled" and result.calls == (expected,)


@pytest.mark.parametrize("prompt", [
    "Read appointments tomorrow calendar next week",
    "Read email yesterday email today",
    "Read email then recap email",
])
def test_alias_reference_grammar_never_hides_unclear_or_missing_repeated_scopes(prompt):
    if "appointments" in prompt:
        bad = value(source("calendar", "records", time={"named": "tomorrow"}), source("calendar", "records", time={"named": "next week"}))
    elif "yesterday" in prompt:
        bad = value(source("email", "records", time={"named": "yesterday"}), source("email", "records", time={"named": "today"}))
    else:
        bad = value(source("email", "records"))
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)
    assert run(prompt, FakeClient(bad, bad)).disposition == "clarify"


def test_alias_coalescing_inside_repeated_clauses_keeps_whole_date_associations():
    prompt = "Read appointments on my calendar tomorrow; read appointments in the calendar next week"
    good = value(source("calendar", "records", time={"named": "tomorrow"}), source("calendar", "records", time={"named": "next week"}))
    assert validate_intent(good, prompt, now=NOW)
    bad = value(source("calendar", "records", time={"named": "tomorrow"}))
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)



MIXED_LOOKUP_PROMPT = "Show my calendar tomorrow; find notes about amber notebooks from yesterday"
MIXED_LOOKUP_GOOD = value(source("calendar", "records", time={"named": "tomorrow"}),
                          source("notes", "records", query="amber notebooks", time={"named": "yesterday"}))
COORDINATED_EMAIL_PROMPT = "Read email from Selene and email from Dorian for yesterday"
COORDINATED_EMAIL_GOOD = value(source("email", "records", query="Selene", time={"named": "yesterday"}),
                              source("email", "records", query="Dorian", time={"named": "yesterday"}))


@pytest.mark.parametrize("prompt,data,expected", [
    (MIXED_LOOKUP_PROMPT, MIXED_LOOKUP_GOOD,
     (("get_upcoming", {"period": "tomorrow", "calendar_only": True}), ("search_notes", {"query": "amber notebooks", "period": "yesterday"}))),
    ("Recap email; find notes named amber notebooks", value(source("email"), source("notes", "records", query="amber notebooks")),
     (("summarize_emails", {}), ("search_notes", {"query": "amber notebooks"}))),
    ("Find notes about amber notebooks and recap email", value(source("notes", "records", query="amber notebooks"), source("email")),
     (("search_notes", {"query": "amber notebooks"}), ("summarize_emails", {}))),
])
def test_lookup_filter_requirement_belongs_only_to_its_source(prompt, data, expected):
    assert validate_intent(data, prompt, now=NOW)
    result = run(prompt, FakeClient(data))
    assert result.disposition == "compiled" and result.calls == expected


@pytest.mark.parametrize("mutation", ["missing", "truncated", "invented", "swapped_date"])
def test_mixed_lookup_filter_fix_retains_exact_filter_and_time_rejection(mutation):
    bad = copy.deepcopy(MIXED_LOOKUP_GOOD)
    if mutation == "missing":
        bad["sources"][1].pop("query")
    elif mutation == "swapped_date":
        bad["sources"][0]["time"], bad["sources"][1]["time"] = bad["sources"][1]["time"], bad["sources"][0]["time"]
    else:
        bad["sources"][1]["query"] = "amber" if mutation == "truncated" else "invented notebook"
    with pytest.raises(InvalidIntent):
        validate_intent(bad, MIXED_LOOKUP_PROMPT, now=NOW)
    assert run(MIXED_LOOKUP_PROMPT, FakeClient(bad, bad)).disposition == "clarify"


def test_literal_source_word_cannot_authorize_an_extra_unfiltered_read():
    bad = value(source("notes", "records", query="my calendar"), source("calendar"))
    with pytest.raises(InvalidIntent):
        validate_intent(bad, "Find notes about my calendar", now=NOW)


@pytest.mark.parametrize("prompt", [
    COORDINATED_EMAIL_PROMPT,
    'Read email from "Selene" and email from "Dorian" for yesterday',
    "Recap texts with Imani and message from Dorian",
    "Read my notes and email about amber notebooks",
])
def test_coordinated_filter_noun_is_not_a_delivery_authorization(prompt):
    from service.router.web_request import classify as parse_web_request
    request = parse_web_request(prompt)
    assert request.delivery is None and not request.authorized_effects
    assert request.source == prompt


@pytest.mark.parametrize("tail,effect", [
    ("email Mom", "send_email"),
    ("email Mom with the summary", "send_email"),
    ("email from Dorian to Mom", "send_email"),
    ("email it to Mom", "send_email"),
    ("send it by email to Mom", "send_email"),
    ("draft an email to Mom", "draft_email"),
    ("forward it by email to Mom", "send_email"),
    ("text Mom", "send_message"),
    ("message it to Mom", "send_message"),
])
def test_genuine_coordinated_delivery_remains_an_authorized_effect(tail, effect):
    from service.router.web_request import classify as parse_web_request
    request = parse_web_request("Read email from Selene and " + tail)
    assert request.delivery is not None and effect in request.authorized_effects


def test_read_noun_does_not_hide_a_later_delivery_or_quoted_and_negated_forms():
    from service.router.web_request import classify as parse_web_request
    prompt = COORDINATED_EMAIL_PROMPT + " and send it by email to Mom"
    assert "send_email" in parse_web_request(prompt).authorized_effects
    assert not parse_web_request('Read email about "email Mom" and email from Dorian').authorized_effects
    assert not parse_web_request("Read email from Selene and do not email Mom").authorized_effects



def test_only_lookup_occurrence_requires_filter_when_same_source_is_repeated():
    prompt = "Show my notes tomorrow; find notes about amber notebooks from yesterday"
    good = value(source("notes", "records", time={"named": "tomorrow"}),
                 source("notes", "records", query="amber notebooks", time={"named": "yesterday"}))
    assert validate_intent(good, prompt, now=NOW)
    assert run(prompt, FakeClient(good)).disposition == "compiled"
    for queries in [("amber notebooks", None), (None, None), (None, "amber")]:
        bad = copy.deepcopy(good)
        for item, query in zip(bad["sources"], queries):
            item.pop("query", None)
            if query:
                item["query"] = query
        with pytest.raises(InvalidIntent):
            validate_intent(bad, prompt, now=NOW)


def test_unbounded_repeated_lookup_does_not_become_an_unfiltered_read():
    bad = value(source("notes", "records"))
    with pytest.raises(InvalidIntent):
        validate_intent(bad, "Show my notes; find notes", now=NOW)


@pytest.mark.parametrize("head", ["Recap", "Review", "Inspect", "Browse", "Scan", "Summarize"])
@pytest.mark.parametrize("noun", ["email", "text", "message"])
def test_read_governors_mask_only_their_source_noun(head, noun):
    from service.workflows.compiler import outbound_verb, compile_new
    prompt = f"{head} {noun}; find notes named harbor sketches"
    assert not outbound_verb(prompt)
    assert compile_new(prompt) is None
    assert outbound_verb(prompt + "; email Mom the summary")
    assert outbound_verb(prompt + "; text Mom saying hello")
    assert outbound_verb(prompt + "; draft an email to Mom")


LITERAL_SOURCE_CASES = [
    ("Find notes about my calendar", "my calendar"),
    ("Find notes about our messages", "our messages"),
    ("Find notes named email reminders", "email reminders"),
    ('Find notes about "my calendar"', "my calendar"),
]


@pytest.mark.parametrize("prompt,query", LITERAL_SOURCE_CASES)
def test_complete_lookup_literal_does_not_request_its_named_sources(prompt, query):
    data = value(source("notes", "records", query=query))
    assert validate_intent(data, prompt, now=NOW)
    assert run(prompt, FakeClient(data)).calls == (("search_notes", {"query": query}),)
    for bad in [value(source("notes", "records")), value(source("notes", "records", query=query.split()[0])),
                value(source("notes", "records", query=query), source("calendar"))]:
        with pytest.raises(InvalidIntent):
            validate_intent(bad, prompt, now=NOW)


def test_literal_source_authority_and_exclusions_have_separate_bounds():
    prompt = "Find notes about my calendar without email or messages"
    data = value(source("notes", "records", query="my calendar"))
    data["excluded_sources"] = ["email", "messages"]
    assert validate_intent(data, prompt, now=NOW)
    assert run(prompt, FakeClient(data)).calls == (("search_notes", {"query": "my calendar"}),)
    bad = copy.deepcopy(data)
    bad["excluded_sources"] = []
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)
    quoted = value(source("notes", "records", query="my calendar without email"))
    assert validate_intent(quoted, 'Find notes about "my calendar without email"', now=NOW)


def test_prior_lookup_literal_cannot_grant_contextual_source_authority():
    context = [{"role": "user", "content": "Find notes about my calendar"}]
    data = value(source("notes", "records", query="my calendar", time={"named": "yesterday"}))
    assert validate_intent(data, "Same for yesterday", context=context, prior_tools=["search_notes"], now=NOW)
    bad = value(source("calendar", time={"named": "yesterday"}))
    with pytest.raises(InvalidIntent):
        validate_intent(bad, "Same for yesterday", context=context, prior_tools=["search_notes"], now=NOW)


PREPOSITION_READ_CASES = [
    ("Read email from Lyra and email about road to recovery", "road to recovery"),
    ("Review email from Lyra and email named guide to gardening", "guide to gardening"),
    ("Read email from Lyra and email containing response to update", "response to update"),
    ('Read email from Lyra and email about "road to recovery"', "road to recovery"),
]


@pytest.mark.parametrize("prompt,query", PREPOSITION_READ_CASES)
def test_coordinated_preposition_within_query_is_not_a_recipient(prompt, query):
    from service.router.web_request import classify
    request = classify(prompt)
    assert request.delivery is None and not request.authorized_effects and request.source == prompt
    data = value(source("email", "records", query="Lyra"), source("email", "records", query=query))
    assert validate_intent(data, prompt, now=NOW)
    result = run(prompt, FakeClient(data))
    assert result.calls == (("view_emails", {"query": "Lyra", "strict_match": True}),
                            ("view_emails", {"query": query, "strict_match": True}))
    bad = copy.deepcopy(data)
    bad["sources"][1]["query"] = query.split()[0]
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)


@pytest.mark.parametrize("tail", ["email from Lyra to Mom", "email about road to recovery to Mom",
                                  "email about roadmap to me", "email about roadmap to person@example.com",
                                  "email about roadmap to +15551234567", "email it to Taylor",
                                  "email about road to recovery and send it by email to Mom"])
def test_query_attachment_fix_keeps_explicit_and_later_destinations(tail):
    from service.router.web_request import classify
    assert classify("Read email from Selene and " + tail).authorized_effects & {"send_email", "draft_email"}


@pytest.mark.parametrize("prompt", ["Find notes about send email and send it to Mom",
                                    'Find notes about "send email" and send it to Mom',
                                    "Find notes about roadmap and forward it by email to Mom"])
def test_complete_literal_mask_never_consumes_a_later_delivery_clause(prompt):
    from service.workflows.compiler import outbound_verb
    from service.router.web_request import classify
    assert outbound_verb(prompt)
    assert classify(prompt).delivery is not None


def test_literal_source_word_does_not_hide_independently_requested_source():
    prompt = "Find notes about my calendar and read email from Lyra"
    good = value(source("notes", "records", query="my calendar"), source("email", "records", query="Lyra"))
    assert validate_intent(good, prompt, now=NOW)
    with pytest.raises(InvalidIntent):
        validate_intent(value(source("notes", "records", query="my calendar")), prompt, now=NOW)


def test_unsupported_literal_time_bounds_do_not_crash_classification_or_hide_action():
    from service.workflows.compiler import outbound_verb
    from service.router.intent.validation import applicable_read
    prompt = "Find notes about plans from last week to next week"
    assert applicable_read(prompt)
    assert run(prompt, FakeClient(value(source("notes", "records", query="plans")), value(source("notes", "records", query="plans")))).disposition == "clarify"
    assert outbound_verb(prompt + " and send it to Mom")


def test_domain_allowlist_uses_instruction_sources_not_complete_literal_words():
    from service.router.intent.validation import source_requirements
    prompt = "Find notes about my calendar without email"
    assert source_requirements(prompt) == ({"notes"}, {"email"})
    config = {**CONFIG, "domains": ["notes"]}
    good = value(source("notes", "records", query="my calendar"))
    good["excluded_sources"] = ["email"]
    result = asyncio.run(plan_read(prompt, client=FakeClient(good), model=MODEL, config=config, now=NOW))
    assert result.disposition == "compiled" and result.calls == (("search_notes", {"query": "my calendar"}),)


GOVERNED_TITLE_CASES = [
    ("Read email from Cassia and email about Guide to Gardening", "Guide to Gardening"),
    ("Read email from Cassia and email about guide to gardening", "guide to gardening"),
    ('Read email from Cassia and email about "Guide to Gardening"', "Guide to Gardening"),
    ("Review email from Cassia and email named Road to Recovery", "Road to Recovery"),
    ("Find notes about maps and email titled Guide to Gardening", "Guide to Gardening"),
    ("Read email from Cassia and email about Topic to Taylor", "Topic to Taylor"),
    ("Read email from Cassia and email about Send Instructions", "Send Instructions"),
]


@pytest.mark.parametrize("prompt,query", GOVERNED_TITLE_CASES)
def test_governed_filter_title_cannot_authorize_recipient_by_capitalization(prompt, query):
    from service.router.web_request import classify
    request = classify(prompt)
    assert request.delivery is None and not request.authorized_effects
    assert request.source == prompt
    first = source("notes", "records", query="maps") if prompt.startswith("Find") else source("email", "records", query="Cassia")
    good = value(first, source("email", "records", query=query))
    assert validate_intent(good, prompt, now=NOW)
    assert run(prompt, FakeClient(good)).disposition == "compiled"
    bad = copy.deepcopy(good)
    bad["sources"][1]["query"] = query.split()[0]
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)


ACTION_TAIL_CASES = [
    ("and", "share it with Mom"),
    ("plus", "share it with Mom"),
    ("plus", "send it by email to Mom"),
    ("and", "would you mind sharing it with Mom"),
    ("plus", "please forward it by email to Mom"),
    ("plus", "save it as a note"),
    ("and then", "please share it with Mom"),
    ("&", "could you share it with Mom"),
    ("but", "share it with Mom"),
    (",", "share it with Mom"),
    ("then", "forward it by email to Mom"),
    ("and afterwards", "please send it to Mom"),
    ("and", "compose an email to Mom"),
    ("and", "save it as a note"),
    ("and", "open it"),
    ("and", "move it to Downloads"),
    ("plus", "please update the reminder"),
    ("and", "cancel the appointment"),
    ("plus", "mark the reminder complete"),
    ("and then", "clear the reminder list"),
]


@pytest.mark.parametrize("joiner,tail", ACTION_TAIL_CASES)
def test_complete_lookup_bounds_retain_every_established_effect_head(joiner, tail):
    from service.router.intent.validation import _instruction_text, applicable_read
    from service.workflows.compiler import outbound_verb
    prompt = "Find notes about cedar maps " + joiner + " " + tail
    masked = _instruction_text(prompt, now=NOW)
    assert tail in masked and "cedar maps" not in masked
    assert not applicable_read(prompt)
    for query in ["cedar maps", "cedar maps " + joiner + " " + tail]:
        with pytest.raises(InvalidIntent):
            validate_intent(value(source("notes", "records", query=query)), prompt, now=NOW)
    if any(verb in tail for verb in ["share", "send", "forward", "compose"]):
        assert outbound_verb(prompt)


@pytest.mark.parametrize("negation", ["don't", "do not", "never"])
def test_negative_share_clauses_are_bound_instructions_without_effect_permission(negation):
    from service.router.web_request import classify
    from service.router.intent.validation import _instruction_text
    prompt = "Find notes about cedar maps and " + negation + " share it with Mom"
    masked = _instruction_text(prompt, now=NOW)
    assert negation + " share it with Mom" in masked
    assert not classify(prompt).authorized_effects
    assert validate_intent(value(source("notes", "records", query="cedar maps")), prompt, now=NOW)
    bad = value(source("notes", "records", query="cedar maps and " + negation + " share it with Mom"))
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)


@pytest.mark.parametrize("query", ["cedar maps and share it with Mom", "research and development",
                                    "draft send and forward", "maps, routes and diagrams"])
def test_quoted_action_or_ordinary_coordination_remains_complete_literal(query):
    prompt = 'Find notes about "' + query + '"'
    good = value(source("notes", "records", query=query))
    assert validate_intent(good, prompt, now=NOW)
    assert run(prompt, FakeClient(good)).calls == (("search_notes", {"query": query}),)


@pytest.mark.parametrize("tail", ["email Taylor with the summary", "email it to Taylor",
    "email about Guide to Gardening to Mom", "email about Guide to Gardening to person@example.com",
    "email about Guide to Gardening to +15551234567", "send it by email to Taylor",
    "share it with Mom", "share it by email to Taylor"])
def test_capitalization_fix_retains_real_delivery_relations_without_guessing_channel(tail):
    from service.router.web_request import classify
    request = classify("Read email from Cassia and " + tail)
    assert request.delivery is not None
    if tail == "share it with Mom":
        assert request.delivery.recipient == "Mom" and request.delivery.channel is None
        assert not request.authorized_effects
    else:
        assert request.authorized_effects & {"send_email", "send_message"}


@pytest.mark.parametrize("tail", ["without sharing it with Mom", "please do not share it with Mom",
                                  "do not forward it by email to Mom", "never send it to Mom"])
def test_inflected_and_polite_negative_effects_have_complete_clause_bounds(tail):
    from service.router.intent.validation import _instruction_text
    from service.router.web_request import classify
    prompt = "Find notes about cedar maps and " + tail
    assert tail in _instruction_text(prompt, now=NOW)
    assert not classify(prompt).authorized_effects
    assert validate_intent(value(source("notes", "records", query="cedar maps")), prompt, now=NOW)


def test_negated_effect_does_not_hide_a_later_positive_effect():
    from service.router.intent.validation import _instruction_text, applicable_read
    prompt = "Find notes about cedar maps and do not share it with Mom and then send it by email to Taylor"
    masked = _instruction_text(prompt, now=NOW)
    assert "do not share" in masked and "send it by email to Taylor" in masked
    assert not applicable_read(prompt)
    with pytest.raises(InvalidIntent):
        validate_intent(value(source("notes", "records", query="cedar maps")), prompt, now=NOW)


def test_unquoted_action_words_inside_one_filter_are_still_literal_data():
    prompt = "Find notes about share it with Mom"
    good = value(source("notes", "records", query="share it with Mom"))
    assert validate_intent(good, prompt, now=NOW)
    assert run(prompt, FakeClient(good)).calls == (("search_notes", {"query": "share it with Mom"}),)


@pytest.mark.parametrize("query", ["Guide to Gardening to Taylor", "Guide to Gardening"])
def test_second_unquoted_named_destination_clarifies_without_effect_permission(query):
    from service.router.web_request import classify
    prompt = "Read email from Cassia and email about Guide to Gardening to Taylor"
    request = classify(prompt)
    assert request.source == prompt and request.delivery is None and not request.authorized_effects
    bad = value(source("email", "records", query="Cassia"), source("email", "records", query=query))
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)
    assert run(prompt, FakeClient(bad, bad)).disposition == "clarify"


def test_quoted_multiple_to_title_and_non_delivery_source_stay_literal():
    query = "Guide to Gardening to Taylor"
    prompt = 'Read email from Cassia and email about "' + query + '"'
    good = value(source("email", "records", query="Cassia"), source("email", "records", query=query))
    assert validate_intent(good, prompt, now=NOW)
    assert run(prompt, FakeClient(good)).disposition == "compiled"
    assert validate_intent(value(source("notes", "records", query=query)), "Find notes about " + query, now=NOW)


@pytest.mark.parametrize("joiner", ["and", "plus", "and then"])
def test_source_exclusion_list_ends_before_independent_positive_read(joiner):
    from service.router.intent.validation import source_requirements
    prompt = "Find notes about cedar maps without email " + joiner + " read messages"
    assert source_requirements(prompt) == ({"notes", "messages"}, {"email"})
    good = value(source("notes", "records", query="cedar maps"), source("messages", "records"))
    good["excluded_sources"] = ["email"]
    assert validate_intent(good, prompt, now=NOW)
    assert run(prompt, FakeClient(good)).disposition == "compiled"
    bad = value(source("notes", "records", query="cedar maps"))
    bad["excluded_sources"] = ["email", "messages"]
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)


def test_source_exclusion_or_list_remains_complete_and_quoted_list_is_data():
    from service.router.intent.validation import source_requirements
    prompt = "Find notes about cedar maps without email or messages"
    assert source_requirements(prompt) == ({"notes"}, {"email", "messages"})
    good = value(source("notes", "records", query="cedar maps"))
    good["excluded_sources"] = ["email", "messages"]
    assert validate_intent(good, prompt, now=NOW)
    quoted = 'Find notes about "cedar maps without email and read messages"'
    assert source_requirements(quoted) == ({"notes"}, set())
    assert validate_intent(value(source("notes", "records", query="cedar maps without email and read messages")), quoted, now=NOW)


NEGATIVE_EFFECT_TAILS = [
    "do not share it with Mom", "never share it with Mom",
    "please do not share it with Mom", "do not forward it by email to Mom",
    "never send it to Mom", "do not email it to Mom", "without sharing it with Mom",
    "would you mind not sharing it with Mom", "do not delete it",
    "I would prefer not to send it to Mom", "I prefer not to forward it by email to Mom",
    "I’d prefer not to share it with Mom",
]

LATER_EFFECT_CASES = [
    ("and", "update my reminders"), ("plus", "please update my reminders"),
    ("and then", "mark my reminders complete"), ("&", "clear my reminders"),
    (";", "update my reminders"), ("and", "send it by email to Taylor"),
]


@pytest.mark.parametrize("tail", NEGATIVE_EFFECT_TAILS)
def test_negative_delivery_preflight_does_not_preempt_an_independent_read(tail):
    from service.workflows.compiler import outbound_verb, compile_new
    prompt = "Find notes about amber route and " + tail
    assert not outbound_verb(prompt)
    assert compile_new(prompt) is None
    assert validate_intent(value(source("notes", "records", query="amber route")), prompt, now=NOW)


@pytest.mark.parametrize("joiner,tail", LATER_EFFECT_CASES)
@pytest.mark.parametrize("negative", ["do not delete it", "please never share it with Mom"])
def test_each_effect_clause_owns_its_negation_and_preserves_later_source_authority(joiner, tail, negative):
    from service.router.intent.validation import applicable_read, source_requirements
    from service.workflows.compiler import outbound_verb
    prompt = "Find notes about amber route and " + negative + " " + joiner + " " + tail
    required, excluded = source_requirements(prompt)
    assert "notes" in required and not excluded
    if "reminders" in tail:
        assert "reminders" in required
    assert not applicable_read(prompt)
    with pytest.raises(InvalidIntent):
        validate_intent(value(source("notes", "records", query="amber route")), prompt, now=NOW)
    if tail.startswith("send"):
        assert outbound_verb(prompt)


@pytest.mark.parametrize("joiner,tail", LATER_EFFECT_CASES)
def test_quoted_negative_and_positive_effect_sequence_remains_one_exact_literal(joiner, tail):
    from service.workflows.compiler import outbound_verb, compile_new
    query = "amber route and do not delete it " + joiner + " " + tail
    prompt = 'Find notes about "' + query + '"'
    assert not outbound_verb(prompt) and compile_new(prompt) is None
    assert validate_intent(value(source("notes", "records", query=query)), prompt, now=NOW)


READ_AFTER_NEGATIVE_JOINERS = ["and", "and then", "plus", "and afterwards", "afterwards", "&"]


@pytest.mark.parametrize("joiner", READ_AFTER_NEGATIVE_JOINERS)
def test_negative_effect_span_ends_before_every_coordinated_read(joiner):
    from service.router.intent.validation import source_requirements
    from service.workflows.compiler import outbound_verb
    prompt = "Find notes about amber route and do not share it with Mom " + joiner + " read my reminders"
    assert source_requirements(prompt) == ({"notes", "reminders"}, set())
    assert not outbound_verb(prompt)
    good = value(source("notes", "records", query="amber route"), source("reminders", "records"))
    assert validate_intent(good, prompt, now=NOW)
    with pytest.raises(InvalidIntent):
        validate_intent(value(source("notes", "records", query="amber route")), prompt, now=NOW)


@pytest.mark.parametrize("joiner", READ_AFTER_NEGATIVE_JOINERS)
def test_preference_governor_does_not_hide_a_later_positive_effect(joiner):
    from service.router.intent.validation import applicable_read
    from service.workflows.compiler import outbound_verb
    prompt = "Find notes about amber route and I would prefer not to share it with Mom " + joiner + " send it by email to Taylor"
    assert outbound_verb(prompt) and not applicable_read(prompt)
    with pytest.raises(InvalidIntent):
        validate_intent(value(source("notes", "records", query="amber route")), prompt, now=NOW)


SOURCE_APOSTROPHES = ["'", "’", "‘", "ʼ", "＇"]
EXCLUSION_DOMAINS = ["calendar", "reminders", "email", "messages", "notes"]


def source_exclusion_case(domain, apostrophe, governor="read"):
    if domain == "notes":
        prefix = "Read email from O’Neill"
        allowed = source("email", "records", query="O’Neill")
        expected = ("view_emails", {"query": "O’Neill", "strict_match": True})
    else:
        prefix = "Find notes about quartz birds"
        allowed = source("notes", "records", query="quartz birds")
        expected = ("search_notes", {"query": "quartz birds"})
    prompt = prefix + " and don" + apostrophe + "t " + governor + " my " + domain
    good = value(allowed, excluded_sources=[domain])
    return prompt, good, expected


@pytest.mark.parametrize("apostrophe", SOURCE_APOSTROPHES)
@pytest.mark.parametrize("domain", EXCLUSION_DOMAINS)
@pytest.mark.parametrize("governor", ["read", "include", "check"])
def test_source_exclusion_normalization_preserves_authority_and_literal_bytes(domain, apostrophe, governor):
    from service.router.intent.validation import source_requirements
    prompt, good, expected = source_exclusion_case(domain, apostrophe, governor)
    allowed = good["sources"][0]["domain"]
    assert source_requirements(prompt) == ({allowed}, {domain})
    intent = validate_intent(good, prompt, now=NOW)
    assert compile_intent(intent, now=NOW)[0] == [expected]
    for omitted, added in [(True, False), (False, True), (True, True)]:
        bad = copy.deepcopy(good)
        if omitted:
            bad["excluded_sources"] = []
        if added:
            bad["sources"].append(source(domain))
        with pytest.raises(InvalidIntent):
            validate_intent(bad, prompt, now=NOW)


@pytest.mark.parametrize("apostrophe", SOURCE_APOSTROPHES)
def test_negative_check_exclusion_beats_inherited_email_authority(apostrophe):
    prompt = "don" + apostrophe + "t check my email"
    with pytest.raises(InvalidIntent):
        validate_intent(value(source("email")), prompt,
                        context=[{"role": "user", "content": "Recap my email"}], prior_tools=["summarize_emails"], now=NOW)


@pytest.mark.parametrize("apostrophe", SOURCE_APOSTROPHES)
def test_source_exclusion_lists_keep_an_independent_later_read(apostrophe):
    from service.router.intent.validation import source_requirements
    prompt = "Find notes about quartz birds don" + apostrophe + "t read my email or messages and afterwards read my reminders"
    assert source_requirements(prompt) == ({"notes", "reminders"}, {"email", "messages"})
    good = value(source("notes", "records", query="quartz birds"), source("reminders", "records"), excluded_sources=["email", "messages"])
    assert validate_intent(good, prompt, now=NOW)
    assert compile_intent(validate_intent(good, prompt, now=NOW), now=NOW)[0] == [
        ("search_notes", {"query": "quartz birds"}), ("search_reminders", {"query": "", "scope": "all"})]


@pytest.mark.parametrize("apostrophe", SOURCE_APOSTROPHES)
def test_quoted_unicode_exclusions_and_names_remain_exact_literal_data(apostrophe):
    from service.router.intent.validation import source_requirements
    query = "O’Neill: don" + apostrophe + "t read my email or messages"
    prompt = 'Find notes about "' + query + '"'
    good = value(source("notes", "records", query=query))
    assert source_requirements(prompt) == ({"notes"}, set())
    assert compile_intent(validate_intent(good, prompt, now=NOW), now=NOW)[0] == [("search_notes", {"query": query})]
    bad = value(source("notes", "records", query=query.replace("O’Neill", "O'Neill")))
    with pytest.raises(InvalidIntent):
        validate_intent(bad, prompt, now=NOW)


@pytest.mark.parametrize("apostrophe", SOURCE_APOSTROPHES)
def test_typed_task_negation_uses_equivalent_spellings_without_rewriting_targets(apostrophe):
    from service.tasks.compiler import compile_task
    assert compile_task("don" + apostrophe + "t check my reminders", now=NOW) is None
    name = "O" + apostrophe + "Neill"
    plan = compile_task('Complete reminder called "' + name + '"', now=NOW)
    assert plan is not None and plan.intent == "reminder.complete"
    assert plan.target.value == name


QUOTED_NEGATION_FORMS = ["do not", "dont", "never"] + ["don" + mark + "t" for mark in SOURCE_APOSTROPHES]


@pytest.mark.parametrize("negative", QUOTED_NEGATION_FORMS)
@pytest.mark.parametrize("verb", ["check", "delete"])
def test_task_quoted_negative_title_is_literal_not_a_governor(negative, verb):
    from service.tasks.compiler import compile_task, _unquoted
    title = "Archive: " + negative + " " + verb + " this item"
    prompt = 'Complete reminder called "' + title + '"'
    masked = _unquoted(prompt)
    assert len(masked) == len(prompt) and title not in masked
    plan = compile_task(prompt, now=NOW)
    assert plan is not None and plan.intent == "reminder.complete"
    assert plan.target.value == title and plan.original_request == prompt
    assert plan.target.original == title
    assert compile_task(negative + ' complete reminder called "' + title + '"', now=NOW) is None


@pytest.mark.parametrize("negative", QUOTED_NEGATION_FORMS)
@pytest.mark.parametrize("verb", ["check", "delete", "complete"])
def test_task_real_negative_governor_is_retained_with_a_quoted_name(negative, verb):
    from service.tasks.compiler import compile_task
    assert compile_task(negative + ' ' + verb + ' reminder called "O’Neill"', now=NOW) is None


@pytest.mark.parametrize("negative", QUOTED_NEGATION_FORMS)
@pytest.mark.parametrize("placement", ["before", "after", "unmatched"])
def test_task_quote_mask_keeps_outside_and_unmatched_negative_instructions(negative, placement):
    from service.tasks.compiler import compile_task, _unquoted, _NEGATED
    if placement == "before":
        prompt = negative + ' complete reminder called "O’Neill"'
    elif placement == "after":
        prompt = 'Complete reminder called "O’Neill" and ' + negative + ' delete my reminders'
    else:
        prompt = 'Complete reminder called "Archive: ' + negative + ' check this item'
    masked = _unquoted(prompt)
    assert len(masked) == len(prompt) and _NEGATED.search(masked)
    assert compile_task(prompt, now=NOW) is None


@pytest.mark.parametrize("apostrophe", SOURCE_APOSTROPHES)
def test_task_quote_mask_does_not_consume_possessive_or_contraction_apostrophes(apostrophe):
    from service.tasks.compiler import _unquoted
    prompt = "O" + apostrophe + "Neill don" + apostrophe + "t check my reminders"
    assert _unquoted(prompt) == prompt
