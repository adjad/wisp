"""Model-only Ask answer quality (service/model_only.py + run_agent(ask_quality=True)).

Pure helpers first, then the real agent loop with scripted clients and the
synthetic registry from test_model_led_integration. No model, no server, no
user data: every source and effect is a synthetic seam.
"""
import asyncio
import copy
import json
from datetime import date

import httpx
import pytest

from service import model_only as mo
from service.agent import loop
from service.router.model_led import DISCOVERY_TOOL, fresh_personal_obligation
from tests.test_model_led_integration import Approval, ScriptedClient, call, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _model_only(monkeypatch):
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    monkeypatch.delenv("WISP_MODEL_ONLY_NARRATION_THINKING", raising=False)
    monkeypatch.setattr(loop, "audit", lambda *a, **k: None)


def ask(script, **kwargs):
    kwargs.setdefault("ask_quality", True)
    kwargs.setdefault("model_led_preload", ["get_upcoming"])
    return run(script, **kwargs)


def tool_messages(request):
    return [m for m in request["messages"] if m.get("role") == "tool"]


def executed(effects, name):
    return [args for tool, args in effects if tool == name]


class ReasoningClient(ScriptedClient):
    """Script items may be (reasoning, response) pairs."""

    async def stream_events(self, model, messages, **kwargs):
        step = len(self.requests)
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        item = self.script[step] if step < len(self.script) else "Synthetic final answer."
        thought, response = item if isinstance(item, tuple) else ("", item)
        if thought:
            yield {"kind": "reasoning", "text": thought}
        message = {"role": "assistant", "content": "", "tool_calls": None}
        if isinstance(response, list):
            message["tool_calls"] = response
        else:
            message["content"] = response
        yield {"kind": "final", "message": message}


class GuardClient(ScriptedClient):
    """Answers HTTP 400 (oMLX's prefill-guard refusal) on the listed steps."""

    def __init__(self, script, refuse_steps):
        super().__init__(script)
        self.refuse_steps = set(refuse_steps)
        self.calls = 0
        self.refused = []

    async def stream_events(self, model, messages, **kwargs):
        index = self.calls
        self.calls += 1
        if index in self.refuse_steps:
            self.refused.append(copy.deepcopy({"messages": messages, **kwargs}))
            request = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
            raise httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))
        async for event in super().stream_events(model, messages, **kwargs):
            yield event


def drive(client, *, prompt="what is up this weej?", history=(), **kwargs):
    events = []

    async def emit(event):
        events.append(copy.deepcopy(event))

    async def go():
        return await loop.run_agent(client, "synthetic-agent", [*history, {"role": "user", "content": prompt}],
                                    emit, Approval(), tools=[], model_led_discovery=True, multi_round=True,
                                    include_memory_context=False, debug=False, ask_quality=True,
                                    **{"model_led_preload": ["get_upcoming"], **kwargs})
    answer = asyncio.run(go())
    return events, answer


# --- pure helpers ---------------------------------------------------------------

def test_strip_think_removes_blocks_and_unclosed_monologue():
    assert mo.strip_think("<think>plan</think>\n\nYou have two events.") == "You have two events."
    assert mo.strip_think("We need to check.</think>Answer.") == "Answer."
    assert mo.strip_think("Answer first.<think>trailing monologue") == "Answer first."
    assert mo.strip_think("\n\nPlain answer.\n") == "Plain answer."


def test_salvage_keeps_only_the_final_answer_paragraphs():
    trace = ("The user wants tomorrow's calendar. I should call get_upcoming.\n\n"
             "Tomorrow you have Team Standup at 9:30 AM and Coffee with Alex at 5 PM.")
    assert mo.salvage_answer(trace) == "Tomorrow you have Team Standup at 9:30 AM and Coffee with Alex at 5 PM."
    assert mo.salvage_answer("The user wants X.\n\nI need to call view_emails first.") == ""
    assert mo.salvage_answer("Let me think about which tool to use.") == ""
    assert mo.salvage_answer("x" * 3000) == ""


def test_dedupe_removes_the_answer_copy_from_reasoning():
    answer = "Tomorrow you have Team Standup at 9:30 AM and Coffee with Alex at 5 PM."
    steps = ["The user wants the calendar. Call the calendar tool.", answer,
             "Checked the results.\n\n" + answer]
    assert mo.dedupe_reasoning(steps, answer) == [
        "The user wants the calendar. Call the calendar tool.", "Checked the results."]
    # A short answer is not treated as a duplicate.
    assert mo.dedupe_reasoning(["Two events."], "You have two events.") == ["Two events."]


def test_compact_results_is_short_readable_and_skips_errors():
    results = [
        ("get_upcoming", "This week: 6 events\n" + "\n".join(f"- **9:{i:02d} AM** — Event {i}" for i in range(20))),
        ("find_place", "(the mapping service timed out — try again in a moment)"),
        ("view_emails", "Account: a@b.c\nFrom: Priya <p@x.test>\nTo: me\nSubject: Notes\n\n" + "body " * 400),
    ]
    text = mo.compact_results(results, {"get_upcoming": "From your calendar", "view_emails": "From your email"})
    assert text.startswith("Here's what I found:")
    assert "From your calendar:" in text and "find_place" not in text and "timed out" not in text
    assert "Account:" not in text and "…and" in text
    assert len(text) <= 1210
    assert mo.compact_results([("calculate", "The user denied this action.")], {}) == ""


@pytest.mark.parametrize("prompt", [
    "What can you do with my calendar?", "What can you do?", "Can you read my email?",
    "How do I connect my Google account to you?", "Can you send texts for me?", "what are you good at"])
def test_capability_questions(prompt):
    assert mo.capability_question(prompt)
    assert not mo.asks_personal_data(prompt)


@pytest.mark.parametrize("prompt", [
    "What's on my calendar tomorrow?", "Did Jordan reply to me?", "How heavy is my backpack?",
    "any new texts?", "Do I have anything this weekend?", "Is it going to rain during my coffee with Alex tomorrow?"])
def test_personal_data_requests(prompt):
    assert mo.asks_personal_data(prompt)
    assert not mo.capability_question(prompt)


@pytest.mark.parametrize("prompt", [
    "Why is the sky blue?", "Write a short thank-you note to my professor for the extension.",
    "How do I make cold brew coffee?", "Tell me a joke.", "hi"])
def test_general_requests_are_not_personal(prompt):
    assert not mo.asks_personal_data(prompt)


def test_argument_fix_hint_suggests_a_repair():
    friday = date(2026, 10, 23)  # a Friday
    rejected = "(error: (couldn't understand the period 'next 48 hours'. Use one of: 'today', ...)"
    hint = mo.argument_fix_hint("get_upcoming", {"period": "next 48 hours"}, rejected, today=friday)
    assert "days=2" in hint and "do NOT ask the user" in hint
    hint = mo.argument_fix_hint("get_upcoming", {"period": "Monday"},
                                "(couldn't understand the period 'Monday'.)", today=friday)
    assert "2026-10-26" in hint
    assert "2026-10-24" in mo.argument_fix_hint("get_upcoming", {"period": "this weekend"},
                                                "(couldn't understand the period 'this weekend')", today=friday)
    # A successful result never gets a hint, whatever its body says.
    assert mo.argument_fix_hint("view_emails", {"period": "today"}, "From: x\nThe launch went as expected; invalid") == ""


def test_guard_rejection_is_a_400_only():
    request = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
    assert mo.guard_rejection(httpx.HTTPStatusError("x", request=request, response=httpx.Response(400, request=request)))
    assert not mo.guard_rejection(httpx.HTTPStatusError("x", request=request, response=httpx.Response(500, request=request)))
    assert not mo.guard_rejection(ValueError("400"))


def test_narration_thinking_flag(monkeypatch):
    monkeypatch.setattr("service.config.models_config", lambda: {})
    assert mo.narration_thinking_enabled() is False
    monkeypatch.setattr("service.config.models_config", lambda: {"model_only_narration_thinking": True})
    assert mo.narration_thinking_enabled() is True
    monkeypatch.setenv("WISP_MODEL_ONLY_NARRATION_THINKING", "0")
    assert mo.narration_thinking_enabled() is False


def test_template_kwargs_think_off_only_when_asked():
    assert loop._template_kwargs("m", no_thinking=True) == {"chat_template_kwargs": {"enable_thinking": True}}
    assert loop._template_kwargs("m", no_thinking=False, think_off=True) == {
        "chat_template_kwargs": {"enable_thinking": False}}


def test_demo_sweep_scorer_and_port_guard():
    from scripts import demo_sweep as sweep
    assert sweep.is_refused_base("http://127.0.0.1:8765") and sweep.is_refused_base("http://localhost:8000")
    assert not sweep.is_refused_base("http://127.0.0.1:8775")
    result = "Weather for San Francisco " + "x" * 300
    assert sweep.raw_dump(result, [{"result": result}])
    assert sweep.raw_dump("**From your email:**\nstuff", [])
    assert not sweep.raw_dump("Sunny and 62°F in San Francisco today.", [{"result": result}])
    assert sweep.think_leak("We need to check the calendar first.")
    assert not sweep.think_leak("You have two events tomorrow.")
    card = sweep.scorecard([{"answer": "", "tools": []}, {"answer": "ok", "tools": [], "error": "boom"}])
    assert card["empty"] == 1 and card["error"] == 1 and card["bad_turns"] == 2


# --- the agent loop ---------------------------------------------------------------

def test_guard_400_retries_once_with_a_smaller_request(synthetic):
    effects, _ = synthetic
    client = GuardClient([[call("get_upcoming", days=1)], "Tomorrow: Workshop at 14:00."], refuse_steps={0})
    events, answer = drive(client)
    assert answer == "Tomorrow: Workshop at 14:00."
    [refused] = client.refused
    retried = client.requests[0]
    # The retry dropped every name-only stub; discovery still reaches the rest.
    assert len(retried["tools"]) < len(refused["tools"])
    assert all("description" in s["function"] for s in retried["tools"])
    assert executed(effects, "get_upcoming") == [{"days": 1}]


def test_second_guard_400_ends_with_what_is_in_hand_not_an_error(synthetic):
    client = GuardClient([[call("get_upcoming", days=1)]], refuse_steps={1, 2})
    events, answer = drive(client)
    assert answer.startswith("Here's what I found:") and "Workshop tomorrow at 14:00" in answer
    assert not any(e["type"] == "error" for e in events)
    client = GuardClient([], refuse_steps={0, 1})
    _, answer = drive(client)
    assert answer == loop._ASK_GUARD_MESSAGE


def test_empty_answer_after_tools_gets_a_summary_not_a_dump(synthetic):
    client, _, answer = ask([[call("get_upcoming", days=1)], "", "", "You have a workshop tomorrow at 2 PM."])
    assert answer == "You have a workshop tomorrow at 2 PM."
    summary_request = client.requests[-1]
    assert summary_request["tools"] == [] and "Do not call any tool" in summary_request["messages"][-1]["content"]
    assert summary_request["chat_template_kwargs"] == {"enable_thinking": False}


def test_failed_summary_falls_back_to_the_compact_view(synthetic):
    _, _, answer = ask([[call("get_upcoming", days=1)], "", "", ""])
    assert answer.startswith("Here's what I found:")
    assert "**" not in answer.split("\n", 1)[0]


def test_empty_content_salvages_only_the_final_answer_from_reasoning(synthetic):
    client = ReasoningClient([[call("get_upcoming", days=1)],
                              ("The user wants the agenda. I should summarize get_upcoming.\n\n"
                               "You have a workshop tomorrow at 2 PM.", "")])
    events, answer = drive(client)
    assert answer == "You have a workshop tomorrow at 2 PM."
    assert "The user wants" not in answer


def test_reasoning_shown_without_the_duplicated_answer(synthetic):
    final = "You have one workshop tomorrow at 2 PM, and nothing else is on the calendar."
    client = ReasoningClient([("Need the calendar.", [call("get_upcoming", days=1)]),
                              (final, final)])
    events, answer = drive(client)
    shown = [e["text"] for e in events if e["type"] == "reasoning"]
    assert answer == final and shown == ["Need the calendar."]


def test_guard_does_not_fire_after_a_personal_source_ran(synthetic):
    prompt = "What's on my calendar today?"
    scope = fresh_personal_obligation(prompt, "")
    assert scope["scope"] == "calendar" and scope["day"] == "today"
    # get_upcoming ran with days=1, which does not name "today": the old guard
    # replaced this grounded answer.
    _, _, answer = ask([[call("get_upcoming", days=1)], "A workshop at 14:00."],
                       prompt=prompt, fresh_personal_scope=scope)
    assert answer == "A workshop at 14:00."


def test_capability_question_is_answered_not_guarded(synthetic):
    prompt = "What can you do with my calendar?"
    client, _, answer = ask(["I can read your agenda, find free time and add events."],
                            prompt=prompt, fresh_personal_scope=fresh_personal_obligation(prompt, ""))
    assert answer == "I can read your agenda, find free time and add events."
    assert len(client.requests) == 1  # no forced re-prompt either


def test_personal_answer_with_no_tool_is_reprompted_with_a_tool_required(synthetic):
    effects, _ = synthetic
    prompt = "What's on my calendar tomorrow?"
    client, _, answer = ask(["You have a dentist appointment at 3 PM.", [call("get_upcoming", days=1)],
                             "Tomorrow: a workshop at 14:00."],
                            prompt=prompt, fresh_personal_scope=fresh_personal_obligation(prompt, ""))
    assert client.requests[1]["tool_choice"] == "required"
    assert "Do not guess" in client.requests[1]["messages"][-1]["content"]
    assert executed(effects, "get_upcoming") == [{"days": 1}]
    assert answer == "Tomorrow: a workshop at 14:00."


def test_unscoped_personal_question_is_reprompted_once_then_accepted(synthetic):
    client, _, answer = ask(["Your backpack weighs 1.2 kg.", "I don't have anything that records that."],
                            prompt="How heavy is my backpack?")
    assert client.requests[1]["tool_choice"] == "required"
    assert answer == "I don't have anything that records that."
    assert len(client.requests) == 2


def test_general_question_is_not_reprompted(synthetic):
    client, _, answer = ask(["Rayleigh scattering."], prompt="Why is the sky blue?")
    assert answer == "Rayleigh scattering." and len(client.requests) == 1


def test_one_tool_is_called_at_most_three_times_per_turn(synthetic):
    effects, _ = synthetic
    script = [[call("get_upcoming", days=d)] for d in (1, 2, 3, 4, 5)] + ["Done."]
    client, _, answer = ask(script)
    assert executed(effects, "get_upcoming") == [{"days": 1}, {"days": 2}, {"days": 3}]
    refused = tool_messages(client.requests[4])[-1]["content"]
    assert "already ran 3 times" in refused


def test_blocked_tool_is_not_retried(synthetic, monkeypatch):
    effects, _ = synthetic
    from service.safety.policy import Decision, Tier
    real = loop.decide

    def decide(category, args, tool=None):
        if tool == "view_emails":
            return Decision(Tier.DENY, "synthetic block")
        return real(category, args, tool=tool)
    monkeypatch.setattr(loop, "decide", decide)
    client, events, _ = ask([[call("view_emails")], [call("view_emails")], [call("view_emails")], "No mail access."],
                            model_led_preload=["view_emails"])
    calls = [e for e in events if e["type"] == "tool_call" and e["name"] == "view_emails"]
    assert len(calls) == 1 and executed(effects, "view_emails") == []
    assert "blocked by policy earlier this turn" in tool_messages(client.requests[2])[-1]["content"]


def test_rejected_argument_comes_back_with_a_repair_hint(synthetic):
    effects, register = synthetic

    async def picky(**args):
        if "period" in args:
            return f"(error: (couldn't understand the period '{args['period']}'. Use one of: 'today', ...)"
        return "Workshop tomorrow at 14:00."
    registry_tool = register("get_upcoming", "assistant_tools", parameters={"type": "object", "properties": {
        "days": {"type": "integer"}, "period": {"type": "string"}}, "additionalProperties": False})
    picky.__module__ = registry_tool.func.__module__
    registry_tool.func = picky
    client, _, answer = ask([[call("get_upcoming", period="next 48 hours")], [call("get_upcoming", days=2)],
                             "Tomorrow: a workshop at 14:00."])
    first = tool_messages(client.requests[1])[-1]["content"]
    assert "days=2" in first and "do NOT ask the user to rephrase" in first
    assert answer == "Tomorrow: a workshop at 14:00."


def test_stubs_are_limited_to_the_rank_and_core(synthetic):
    _, register = synthetic
    for name in ("obscure_tool_a", "obscure_tool_b"):
        register(name, "everyday", "compute")
    client, _, _ = ask(["Hi."], model_led_stub_rank=["obscure_tool_a"])
    offered = {s["function"]["name"] for s in client.requests[0]["tools"]}
    assert "obscure_tool_a" in offered and "obscure_tool_b" not in offered
    assert {"view_emails", "view_messages", DISCOVERY_TOOL} <= offered   # core stubs + discovery
    # Without ask_quality every admitted tool is still offered.
    client, _, _ = run(["Hi."], model_led_preload=["get_upcoming"], model_led_stub_rank=["obscure_tool_a"])
    assert "obscure_tool_b" in {s["function"]["name"] for s in client.requests[0]["tools"]}


def test_narration_step_runs_thinking_off_by_default(synthetic, monkeypatch):
    client, _, _ = ask([[call("get_upcoming", days=1)], "Tomorrow: a workshop."])
    assert client.requests[0]["chat_template_kwargs"] == {"enable_thinking": True}
    assert client.requests[1]["chat_template_kwargs"] == {"enable_thinking": False}
    monkeypatch.setenv("WISP_MODEL_ONLY_NARRATION_THINKING", "1")
    client, _, _ = ask([[call("get_upcoming", days=1)], "Tomorrow: a workshop."])
    assert client.requests[1]["chat_template_kwargs"] == {"enable_thinking": True}


def test_compact_catalog_and_step_budget(synthetic):
    history = []
    for i in range(30):
        history += [{"role": "user", "content": f"old question {i} " + "lorem ipsum " * 150},
                    {"role": "assistant", "content": f"old answer {i} " + "dolor sit " * 150}]
    client, _, _ = ask(["Fine."], history=history)
    request = client.requests[0]
    assert "TOOLS (model-led)" in request["messages"][1]["content"]
    estimate = loop._est_tokens(request["tools"]) + sum(loop._est_tokens(m.get("content") or "")
                                                        for m in request["messages"])
    assert estimate <= mo.ASK_PROMPT_BUDGET + 200
    assert request["messages"][-1]["content"] == "what is up this weej?"
    # The baseline loop keeps far more of the same history.
    client, _, _ = run(["Fine."], history=history, model_led_preload=["get_upcoming"])
    assert len(client.requests[0]["messages"]) > len(request["messages"])


def test_ask_quality_off_keeps_the_raw_merge(synthetic):
    _, _, answer = run([[call("get_upcoming", days=1)], "", ""], model_led_preload=["get_upcoming"])
    assert "Workshop tomorrow at 14:00" in answer and not answer.startswith("Here's what I found")
