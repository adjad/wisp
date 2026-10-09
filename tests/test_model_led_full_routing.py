"""Full request ownership; model output is scripted, never an accuracy score."""
import asyncio
import json
import pytest

from service import main
from service.agent import loop
from service.config.endpoints import Target
from service.router.model_led import model_led_enabled, personal_communication_request
from service.tools import registry
from tests.test_model_led_entrypoint import FakeClient, endpoint, tool_call
from tests.test_model_led_integration import synthetic, run, call, discover
from service.tasks.reply_engine import prepare_task_turn_async as real_task_recovery
from service.workflows.engine import prepare_turn as real_workflow_recovery


def waiting_title(endpoint):
    from datetime import datetime, timedelta
    from service.tasks.models import TaskPlan, TemporalValue
    sid = endpoint.store.create_session()
    plan = TaskPlan(temporal=TemporalValue(absolute_iso=(datetime.now() + timedelta(days=2)).isoformat(timespec="minutes")))
    plan.recompute_status()
    endpoint.store.save_workflow(sid, plan.to_dict())
    return sid, plan


def waiting_reply(endpoint):
    from service.tasks.models import TaskPlan, SlotValue
    sid = endpoint.store.create_session()
    plan = TaskPlan(kind="task.email.reply", intent="email.reply", channel=SlotValue("email", "explicit"),
                    parameters={"reply_all": SlotValue(False)},
                    resolved_references={"reply.target": {"fields": {"message_id": "fixture-message", "account": "Fixture"}}})
    plan.recompute_status()
    endpoint.store.save_workflow(sid, plan.to_dict())
    return sid, plan


def continuation_response(plan, decision="continue", **changes):
    return json.dumps({"decision": decision, "owner_id": plan.id,
                       "revision": plan.revision, **changes})


def interpreted_continuation(endpoint, plan, prompt):
    from service.tasks.reply_engine import interpret_owner_continuation
    client = FakeClient(endpoint.target, [continuation_response(plan)])
    admission, verdict = asyncio.run(interpret_owner_continuation(
        client, endpoint.target.model, plan.to_dict(), prompt))
    assert verdict == "continue" and admission is not None
    assert client.requests[0]["messages"][-1]["content"] == prompt
    assert client.requests[0]["tools"] == []
    asyncio.run(client.aclose())
    return admission


@pytest.mark.parametrize("mode", [None, "1"])
@pytest.mark.parametrize("prompt", ["summarize my messages", "read my messages", "list my calendar",
                                   "please sumarize my messages", "could you please read my messages", "reed my messages",
                                   "Give me an overview of my calendar", "Walk me through my schedule tomorrow",
                                   "Bring me up to date on my messages", "I'd like to know what's on this weej",
                                   "My calendar needs a quick overview", "Let me see tomorrow's events"])
def test_new_reads_do_not_fill_or_execute_waiting_reminder(endpoint, monkeypatch, mode, prompt):
    from service.tasks import engine
    if mode is None:
        monkeypatch.delenv("WISP_MODEL_LED_ROUTING")
    monkeypatch.setattr("service.tasks.reply_engine.prepare_task_turn_async", real_task_recovery)
    monkeypatch.setattr(main, "prepare_turn", real_workflow_recovery)
    def no_native(*args, **kwargs):
        raise AssertionError("Unrelated turn touched an old owner's native capability")
    async def no_execution(*args, **kwargs):
        no_native()
    monkeypatch.setattr(engine, "_default_contacts_resolver", no_native)
    monkeypatch.setattr(main, "execute_task", no_execution)
    sid, plan = waiting_title(endpoint)
    endpoint.script = [continuation_response(plan, "new_request")]
    before = endpoint.store.active_task(sid)
    events = endpoint.request(prompt, sid)
    assert endpoint.store.active_task(sid) == before
    assert not endpoint.effects and not endpoint.baseline_calls
    assert endpoint.agent_calls[0]["messages"][-1]["content"] == prompt
    interpretation = endpoint.owned[0].requests[0]
    assert interpretation["tools"] == [] and interpretation["messages"][-1]["content"] == prompt
    assert any(e.get("route_source") == "model_led_discovery" for e in events)


def test_literal_title_still_finishes_exact_waiting_owner(endpoint, monkeypatch):
    from service.tasks import engine
    sid, plan = waiting_title(endpoint)
    def no_contact(*args):
        raise AssertionError("A literal local reminder title triggered a contact read")
    turn = engine.prepare_task_turn(endpoint.store, sid, "Pick up groceries", assistant_store=None,
                                    owner_only=True, contacts_resolver=no_contact,
                                    owner_admission=interpreted_continuation(endpoint, plan, "Pick up groceries"))
    assert turn.executable and turn.plan.id == plan.id
    assert turn.plan.steps[0].args["title"] == "Pick up groceries"


@pytest.mark.parametrize("mode", [None, "1"])
@pytest.mark.parametrize("prompt", ["do not read Mail; explain quantum mechanics",
                                   "do not read email; summarize my messages", "please read my messages"])
def test_pending_reply_never_warms_excluded_or_unrelated_mail(endpoint, monkeypatch, mode, prompt):
    if mode is None:
        monkeypatch.delenv("WISP_MODEL_LED_ROUTING")
    monkeypatch.setattr("service.tasks.reply_engine.prepare_task_turn_async", real_task_recovery)
    monkeypatch.setattr(main, "prepare_turn", real_workflow_recovery)
    def forbidden(*args, **kwargs):
        raise AssertionError("Excluded/unrelated reply recovery source was touched")
    async def forbidden_async(*args, **kwargs):
        forbidden()
    monkeypatch.setattr("service.tools.email_tools.ensure_reply_source", forbidden_async)
    monkeypatch.setattr("service.tasks.source_readers.current_mail_reader", forbidden)
    monkeypatch.setattr("service.tools.action_tools.prepare_reply_args", forbidden_async)
    monkeypatch.setattr(main, "execute_task", forbidden_async)
    sid, _ = waiting_reply(endpoint)
    before = endpoint.store.active_task(sid)
    endpoint.request(prompt, sid)
    assert endpoint.store.active_task(sid) == before and not endpoint.effects
    assert endpoint.agent_calls and not endpoint.baseline_calls
    if "do not read" in prompt:
        assert "view_emails" in endpoint.agent_calls[0]["forbidden_tools"]


def test_allowed_literal_reply_body_preserves_bound_source_without_native_effect(endpoint):
    import asyncio
    sid, plan = waiting_reply(endpoint)
    turn = asyncio.run(real_task_recovery(endpoint.store, sid, "Thanks for the update.",
        assistant_store=None, owner_only=True, mail_reader=object(), allow_native=False,
        owner_admission=interpreted_continuation(endpoint, plan, "Thanks for the update.")))
    assert turn.plan.id == plan.id and turn.plan.subject.value == "Thanks for the update."
    assert turn.plan.resolved_references == plan.resolved_references
    assert turn.event == "reply_prepare" and not turn.executable


@pytest.mark.parametrize("mode", [None, "1"])
def test_literal_title_is_model_interpreted_before_exact_owner_execution(endpoint, monkeypatch, mode):
    from types import SimpleNamespace
    if mode is None:
        monkeypatch.delenv("WISP_MODEL_LED_ROUTING")
    monkeypatch.setattr("service.tasks.reply_engine.prepare_task_turn_async", real_task_recovery)
    monkeypatch.setattr(main, "prepare_turn", real_workflow_recovery)
    sid, plan = waiting_title(endpoint)
    endpoint.script = [continuation_response(plan)]
    async def execute(task, *args, **kwargs):
        assert task.id == plan.id
        assert task.steps[0].tool == "add_reminder"
        assert task.steps[0].args["title"] == "Pick up groceries"
        endpoint.effects.append((task.id, task.steps[0].args.copy()))
        return SimpleNamespace(finalize=True, status="completed", response="Synthetic reminder completed.", tool_calls=[])
    monkeypatch.setattr(main, "execute_task", execute)
    events = endpoint.request("Pick up groceries", sid)
    assert len(endpoint.effects) == 1 and not endpoint.agent_calls and not endpoint.baseline_calls
    assert len(endpoint.owned) == 1 and endpoint.owned[0].closed
    assert endpoint.owned[0].requests[0]["messages"][-1]["content"] == "Pick up groceries"
    assert any(event.get("event") == "execution_started" for event in events)


@pytest.mark.parametrize("prompt", ["Pick up groceries", '"Buy milk"', "Give me an overview of my calendar"])
def test_direct_owner_recovery_requires_model_binding_for_free_form_text(endpoint, prompt):
    from service.tasks import engine
    sid, _ = waiting_title(endpoint)
    before = endpoint.store.active_task(sid)
    assert engine.prepare_task_turn(endpoint.store, sid, prompt, assistant_store=None, owner_only=True) is None
    assert asyncio.run(real_task_recovery(endpoint.store, sid, prompt, assistant_store=None,
                                         owner_only=True, allow_native=False)) is None
    assert endpoint.store.active_task(sid) == before


@pytest.mark.parametrize("defect", ["owner", "revision", "slots", "prompt"])
def test_stale_or_retargeted_continuation_binding_does_not_advance_owner(endpoint, defect):
    from dataclasses import replace
    from service.tasks import engine
    sid, plan = waiting_title(endpoint)
    admission = interpreted_continuation(endpoint, plan, "Pick up groceries")
    changes = {"owner": {"owner_id": "another-owner"}, "revision": {"revision": plan.revision + 1},
               "slots": {"missing_slots": ("recipient",)}, "prompt": {"prompt": "Buy milk"}}
    before = endpoint.store.active_task(sid)
    assert engine.prepare_task_turn(endpoint.store, sid, "Pick up groceries", assistant_store=None,
        owner_only=True, owner_admission=replace(admission, **changes[defect])) is None
    assert endpoint.store.active_task(sid) == before


@pytest.mark.parametrize("reply", ["not JSON", "{}", '```json\n{}\n```', "tool_call", "wrong_owner", "boolean_revision", "ambiguous"])
def test_unclear_model_continuation_keeps_owner_and_mutations_closed(endpoint, monkeypatch, reply):
    from service.tasks import engine
    monkeypatch.setattr("service.tasks.reply_engine.prepare_task_turn_async", real_task_recovery)
    monkeypatch.setattr(main, "prepare_turn", real_workflow_recovery)
    async def no_execution(*args, **kwargs):
        raise AssertionError("Unclear model decision constructed an old effect")
    monkeypatch.setattr(main, "execute_task", no_execution)
    monkeypatch.setattr(engine, "_default_contacts_resolver", lambda *a: (_ for _ in ()).throw(AssertionError("Native lookup")))
    async def fake_mutation(**args):
        raise AssertionError("Unclear continuation admitted a mutation")
    fake_mutation.__module__ = "service.tools.assistant_tools"
    registry.REGISTRY["add_reminder"] = registry.Tool("add_reminder", "Synthetic mutation", {"type": "object"},
                                                   "assistant_write", fake_mutation)
    sid, plan = waiting_title(endpoint)
    before = endpoint.store.active_task(sid)
    answers = {"tool_call": [tool_call("add_reminder", title="Wrong")],
               "wrong_owner": continuation_response(plan, owner_id="different"),
               "boolean_revision": continuation_response(plan, revision=True),
               "ambiguous": continuation_response(plan, "ambiguous")}
    endpoint.script = [answers.get(reply, reply)]
    endpoint.request("Pick up groceries", sid)
    assert endpoint.store.active_task(sid) == before
    assert not endpoint.effects and endpoint.agent_calls and not endpoint.baseline_calls
    assert "add_reminder" in endpoint.agent_calls[0]["forbidden_tools"]
    assert any("Ask whether" in message.get("content", "") for message in endpoint.agent_calls[0]["messages"])


@pytest.mark.parametrize("control", ["cancel", "retry"])
def test_exact_existing_owner_controls_need_no_model_guess(endpoint, control):
    from service.tasks import engine
    sid, plan = waiting_title(endpoint)
    turn = engine.prepare_task_turn(endpoint.store, sid, control, assistant_store=None, owner_only=True)
    if control == "cancel":
        assert turn and turn.plan.id == plan.id and turn.plan.status == "cancelled"
    else:
        assert turn and not turn.executable and turn.plan.id == plan.id
        assert turn.plan.subject.value is None and turn.plan.missing_slots == ["subject"]
        assert endpoint.store.active_task(sid)["id"] == plan.id


@pytest.mark.parametrize("mode", [None, "1"])
def test_revision_changed_during_interpretation_keeps_fallback_mutations_closed(endpoint, monkeypatch, mode):
    from service.tasks import reply_engine
    if mode is None:
        monkeypatch.delenv("WISP_MODEL_LED_ROUTING")
    original_interpretation = reply_engine.interpret_owner_continuation
    monkeypatch.setattr(reply_engine, "prepare_task_turn_async", real_task_recovery)
    monkeypatch.setattr(main, "prepare_turn", real_workflow_recovery)
    sid, plan = waiting_title(endpoint)
    endpoint.script = [continuation_response(plan)]
    changed = []
    async def revise_while_waiting(*args, **kwargs):
        result = await original_interpretation(*args, **kwargs)
        newer = endpoint.store.active_task(sid)
        newer["revision"] += 1
        endpoint.store.save_workflow(sid, newer)
        changed.append(endpoint.store.active_task(sid))
        return result
    monkeypatch.setattr(reply_engine, "interpret_owner_continuation", revise_while_waiting)
    async def no_execution(*args, **kwargs):
        raise AssertionError("Stale continuation triggered execution")
    monkeypatch.setattr(main, "execute_task", no_execution)
    async def no_mutation(**args):
        raise AssertionError("Stale continuation admitted a new effect")
    no_mutation.__module__ = "service.tools.assistant_tools"
    registry.REGISTRY["add_reminder"] = registry.Tool("add_reminder", "Synthetic mutation", {"type": "object"},
                                                   "assistant_write", no_mutation)
    endpoint.request("Pick up groceries", sid)
    assert changed and endpoint.store.active_task(sid) == changed[0]
    assert endpoint.store.active_task(sid)["subject"]["value"] is None
    assert endpoint.agent_calls[0]["messages"][-1]["content"] == "Pick up groceries"
    assert "add_reminder" in endpoint.agent_calls[0]["forbidden_tools"]
    assert not endpoint.effects and not endpoint.baseline_calls


@pytest.mark.parametrize("mode", [None, "1"])
def test_cancelled_uncertain_legacy_owner_keeps_model_action_envelope_closed(endpoint, monkeypatch, mode):
    from service.workflows.models import WorkflowPlan
    if mode is None:
        monkeypatch.delenv("WISP_MODEL_LED_ROUTING")
    async def forbidden_shell(**args):
        raise AssertionError("An unresolved effect admitted another mutation")
    forbidden_shell.__module__ = "service.tools.builtin"
    registry.REGISTRY["run_shell"] = registry.Tool("run_shell", "Synthetic mutation",
        {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        "shell", forbidden_shell)
    sid = endpoint.store.create_session()
    plan = WorkflowPlan(status="running", sources=["messages"], recipient="Fixture", channel="messages", revision=1)
    endpoint.store.save_workflow(sid, plan.to_dict())
    assert endpoint.store.claim_workflow_effect(sid, plan.id, f"workflow_effect:{plan.id}", revision=1)
    assert real_workflow_recovery(endpoint.store, sid, "cancel", owner_only=True).plan.status == "cancelled"
    monkeypatch.setattr(main, "prepare_turn", real_workflow_recovery)
    endpoint.request("send a new message to another person", sid)
    assert endpoint.store.unresolved_action(sid)
    assert "run_shell" in endpoint.agent_calls[0]["forbidden_tools"]
    assert not endpoint.effects


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
