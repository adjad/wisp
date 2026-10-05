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
