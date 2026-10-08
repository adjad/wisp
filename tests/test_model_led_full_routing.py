"""Full request ownership; model output is scripted, never an accuracy score."""
import pytest

from service import main
from service.agent import loop
from service.config.endpoints import Target
from service.router.model_led import model_led_enabled, personal_communication_request
from service.tools import registry
from tests.test_model_led_entrypoint import endpoint, tool_call
from tests.test_model_led_integration import synthetic, run, call, discover


def test_default_routes_through_configured_model_even_after_rename(endpoint, monkeypatch):
    monkeypatch.delenv("WISP_MODEL_LED_ROUTING")
    endpoint.target = Target("agent", endpoint.target.endpoint, "renamed-trained-model")
    endpoint.request("what is up this weej")
    assert endpoint.agent_calls[0]["model"] == "renamed-trained-model"
    assert not endpoint.baseline_calls


@pytest.mark.parametrize("prompt", ["yes", "Messages", "and tomorrow?", "what did mom say today", "what is up this weej"])
def test_model_interprets_followups_without_positive_router_or_new_compiler(endpoint, monkeypatch, prompt):
    def forbidden(*args, **kwargs):
        raise AssertionError("Positive host selection ran")
    async def forbidden_async(*args, **kwargs):
        forbidden()
    monkeypatch.setattr(main, "route", forbidden_async)
    monkeypatch.setattr("service.tasks.reply_engine.prepare_task_turn_async", forbidden_async)
    monkeypatch.setattr("service.workflows.reads.compile_read", forbidden)
    monkeypatch.setattr(main, "prepare_turn", forbidden)
    sid = endpoint.store.create_session()
    endpoint.store.add_turn(sid, "assistant", "Messages or email?")
    endpoint.request(prompt, sid)
    assert endpoint.agent_calls[0]["messages"][-1]["content"] == prompt


def test_family_communication_never_admits_public_egress(endpoint):
    endpoint.request("what did mom say today")
    assert {"web_search", "web_fetch", "run_shell", "http_request"} <= set(
        endpoint.agent_calls[0]["forbidden_tools"])


@pytest.mark.parametrize("prompt", ["search the web for what Mom said", "What did Einstein's mother say?",
                                      'Explain the phrase "what did mom say"'])
def test_public_or_quoted_text_is_not_a_personal_communication_guard(prompt):
    assert not personal_communication_request(prompt)


@pytest.mark.parametrize("mode", ["1", None])
def test_mom_lookup_uses_selected_synthetic_schema_and_fresh_evidence(endpoint, monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("WISP_MODEL_LED_ROUTING")
    monkeypatch.setattr(main, "run_agent", loop.run_agent)
    monkeypatch.setattr(loop, "_BLOCKS_CACHE", None)
    monkeypatch.setattr(loop, "narration_mode", lambda: "off")
    monkeypatch.setattr(loop, "no_thinking_kwargs", lambda *args, **kwargs: {})
    monkeypatch.setattr("service.memory.identity.identity_prompt_block", lambda **kwargs: "")
    monkeypatch.setattr("service.skills.skills_context_block", lambda *args: "")
    monkeypatch.setattr("service.safety.grants.check", lambda *args: None)
    monkeypatch.setattr("service.safety.policy._FULL_ACCESS", False)
    monkeypatch.setattr("service.safety.policy._READ_ONLY", False)
    async def messages(**args):
        endpoint.effects.append(("view_messages", args))
        return "[Thu Oct 8, 1:00 PM] Mom — Mom: meet at six."
    messages.__module__ = "service.tools.imessage_tools"
    registry.REGISTRY["view_messages"] = registry.Tool("view_messages", "Synthetic messages read",
        {"type": "object", "properties": {"query": {"type": "string"},
         "period": {"type": "string"}}, "required": ["query", "period"]}, "messages_read", messages)
    endpoint.script = [[tool_call("get_tool_schemas", families=["messages_contacts"], tools=["view_messages"])],
                       [tool_call("view_messages", query="Mom", period="today")], "Mom said: meet at six."]
    sid = endpoint.store.create_session()
    endpoint.store.add_turn(sid, "assistant", "Yesterday's old synthetic summary.")
    events = endpoint.request("what did mom say today", sid)
    assert endpoint.effects == [("view_messages", {"query": "Mom", "period": "today"})]
    assert any(e.get("text") == "Mom said: meet at six." for e in events)
    assert not endpoint.baseline_calls


@pytest.mark.parametrize("value", ["", "probably", "enabled"])
def test_invalid_mode_override_never_silently_uses_rules(value):
    with pytest.raises(ValueError):
        model_led_enabled(value)


def test_current_personal_answer_without_fresh_read_is_replaced_by_clarification(synthetic):
    _, _, answer = run(["Mom said the old stale thing."], prompt="what did mom say today",
                       require_fresh_personal=True)
    assert "old stale" not in answer and answer.endswith("?")


def test_source_failure_or_unrelated_public_result_does_not_ground_personal_answer(synthetic):
    _, register = synthetic
    register("web_search", "web_tools", "web_read")
    _, _, answer = run([[discover("public_web", "web_search")], [call("web_search")],
                       "Mom said public synthetic news."], prompt="what did mom say today",
                       require_fresh_personal=True)
    assert "public synthetic news" not in answer and answer.endswith("?")


def test_personal_clarification_can_be_asked_without_reading(synthetic):
    _, _, answer = run(["Which channel should I check?"], require_fresh_personal=True)
    assert answer == "Which channel should I check?"


def test_existing_owner_only_task_mode_never_compiles_new_request(endpoint, monkeypatch):
    import asyncio
    from service.tasks import compiler, engine, reply_engine
    def forbidden(*args, **kwargs):
        raise AssertionError("Fresh positive task compilation")
    monkeypatch.setattr(compiler, "compile_task", forbidden)
    monkeypatch.setattr(engine, "compile_task", forbidden)
    sid = endpoint.store.create_session()
    result = asyncio.run(reply_engine.prepare_task_turn_async(endpoint.store, sid,
        "reply to the newest email", assistant_store=None, owner_only=True, allow_native=False))
    assert result is None


def test_existing_owner_only_workflow_mode_never_compiles_new_request(endpoint, monkeypatch):
    from service.workflows import engine
    def forbidden(*args, **kwargs):
        raise AssertionError("Fresh positive workflow compilation")
    monkeypatch.setattr(engine, "compile_new", forbidden)
    sid = endpoint.store.create_session()
    assert engine.prepare_turn(endpoint.store, sid, "send my calendar to Mom", owner_only=True) is None


def test_rollback_cannot_bypass_uncertain_model_action(endpoint, monkeypatch):
    sid = endpoint.store.create_session()
    claim = endpoint.store.claim_model_effect(sid, "prior", "send_message", {"to": "fixture"}, outbound=True)
    endpoint.store.finish_model_effect(sid, claim, verified=False)
    monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "0")
    events = endpoint.request("send that to another person", sid)
    assert not endpoint.baseline_calls and not endpoint.agent_calls and not endpoint.effects
    assert any("unverified outcome" in e.get("text", "") for e in events)


def test_memory_exclusion_removes_automatic_memory_context(endpoint):
    endpoint.request("what did mom say today but do not use memory")
    assert endpoint.agent_calls[0]["include_memory_context"] is False
    assert {"recall", "search_conversations", "remember", "forget", "clear_memory"} <= set(
        endpoint.agent_calls[0]["forbidden_tools"])


def test_active_reminder_cannot_hijack_new_personal_question(endpoint, monkeypatch):
    from service.tasks import engine
    sid = endpoint.store.create_session()
    old = engine.prepare_task_turn(endpoint.store, sid, "remind me", assistant_store=None)
    assert old and old.plan.status == "waiting_for_input"
    before = endpoint.store.active_task(sid)
    def forbidden(*args, **kwargs):
        raise AssertionError("New task compilation while recovering old owner")
    monkeypatch.setattr(engine, "compile_task", forbidden)
    assert engine.prepare_task_turn(endpoint.store, sid, "what did Mom say today", assistant_store=None,
                                    owner_only=True) is None
    assert endpoint.store.active_task(sid) == before


def test_active_workflow_only_advances_bound_slot_and_never_compiles_new_payload(endpoint, monkeypatch):
    from service.workflows import engine
    sid = endpoint.store.create_session()
    prior = engine.prepare_turn(endpoint.store, sid, "send Mom my calendar")
    assert prior and prior.plan.status == "waiting_for_channel"
    before = endpoint.store.active_workflow(sid)
    def forbidden(*args, **kwargs):
        raise AssertionError("New payload compilation during owner recovery")
    monkeypatch.setattr(engine, "compile_new", forbidden)
    assert engine.prepare_turn(endpoint.store, sid, "what did Mom say today", owner_only=True) is None
    assert endpoint.store.active_workflow(sid) == before
    continuation = engine.prepare_turn(endpoint.store, sid, "Messages", owner_only=True)
    assert continuation and continuation.plan.id == prior.plan.id
    assert continuation.plan.channel == "messages"


def test_unrelated_calendar_read_cannot_ground_a_mom_answer(synthetic):
    _, _, answer = run([[discover("calendar", "get_upcoming")], [call("get_upcoming", period="today")],
                       "Mom said a stale thing."], prompt="what did Mom say today",
                       fresh_personal_scope={"scope": "communications", "contact": "mom", "day": "today"})
    assert "stale thing" not in answer and answer.endswith("?")


def test_body_mention_and_outgoing_message_are_not_contact_proof():
    from types import SimpleNamespace
    from service.router.model_led import personal_evidence_matches
    proof = {"scope": "communications", "contact": "mom", "day": "today"}
    tool = SimpleNamespace(name="view_messages", category="messages_read")
    for text in ("[Thu Oct 8, 1:00 PM] Bob — Bob: Mom said six.",
                 "[Thu Oct 8, 1:00 PM] Mom — Me: meet six.",
                 "[Thu Oct 8, 1:00 PM] Bob — Bob: quoted header:\n[Thu Oct 8, 1:00 PM] Mom — Mom: six."):
        assert not personal_evidence_matches(proof, tool, {"query": "Mom", "day": "today"}, {}, text)
    assert personal_evidence_matches(proof, tool, {"query": "Mom", "day": "today"}, {},
                                    "[Thu Oct 8, 1:00 PM] Mom — Mom: six.")
    mail = SimpleNamespace(name="view_emails", category="email_read")
    assert not personal_evidence_matches(proof, mail, {"query": "Mom", "day": "today"}, {},
        "From: Bob\nTo: fixture\nSubject: Mom\nDate: today\nMom said six.")


def test_question_with_unverified_factual_premise_is_replaced(synthetic):
    _, _, answer = run(["What time was the six o'clock meeting Mom mentioned?"],
                       require_fresh_personal=True)
    assert "six o'clock" not in answer and "haven't checked" in answer


@pytest.mark.parametrize("name,category,text", [
    ("view_messages", "messages_read", "Wisp is still syncing your messages after launch, so I’m holding off."),
    ("view_messages", "messages_read", "(Can't read Messages: fixture permission unavailable.)"),
    ("view_emails", "email_read", "(No raw email content cached right now — either Mail.app hasn't synced yet.)"),
    ("view_emails", "email_read", "(Read/unread status isn't in the raw email cache yet — arrives after sync.)")])
def test_source_readiness_failure_does_not_establish_freshness(name, category, text):
    from types import SimpleNamespace
    from service.router.model_led import personal_evidence_matches
    scope = "messages" if category == "messages_read" else "mail"
    assert not personal_evidence_matches({"scope": scope, "day": "today"},
        SimpleNamespace(name=name, category=category), {"day": "today"}, {}, text)


def test_genuine_current_empty_messages_scope_is_distinct_from_sync_failure():
    from types import SimpleNamespace
    from service.router.model_led import personal_evidence_matches
    assert personal_evidence_matches({"scope": "messages", "day": "today"},
        SimpleNamespace(name="view_messages", category="messages_read"), {"day": "today"}, {},
        "No messages found for today.")


@pytest.mark.parametrize("prompt", ["and tommrow?", "what about tomorrow?", "for today", "yesterday instead"])
def test_short_date_followup_retains_personal_evidence_obligation(prompt):
    from service.router.model_led import fresh_personal_obligation
    assert fresh_personal_obligation(prompt, "get_upcoming")["scope"] == "calendar"


@pytest.mark.parametrize("prompt", ["what is the weather tomorrow?", "tell me tomorrow's news",
                                   "calculate tomorrow's sales projection", "what did Einstein say yesterday?"])
def test_new_topic_does_not_inherit_prior_calendar_date_obligation(prompt):
    from service.router.model_led import fresh_personal_obligation
    assert fresh_personal_obligation(prompt, "get_upcoming") == {}
