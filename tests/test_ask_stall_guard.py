"""The stall guard: a model-led Ask final that only promises to look ("I'll look
up the best details on that for you right now.") while nothing ran and tools
were offered gets one neutral nudge, and its next reply is accepted as is.
Toolbench chat_ram_what_for and mail_water_shutoff ended on exactly that."""
import pytest

from service import model_only as mo
from service.agent import loop
from tests.test_model_led_integration import call, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

RAM_STALL = ("It looks like you're curious about what RAM is and what it does! "
             "I'll look up the best details on that for you right now.")
NUDGE = "You said you would look, but nothing has run yet."


@pytest.fixture(autouse=True)
def _model_only(monkeypatch):
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    monkeypatch.delenv("WISP_MODEL_ONLY_NARRATION_THINKING", raising=False)
    monkeypatch.setattr(loop, "audit", lambda *a, **k: None)


def nudged(request):
    last = request["messages"][-1]
    return last["role"] == "user" and NUDGE in last["content"]


@pytest.mark.parametrize("text", [
    RAM_STALL,
    "I'm looking up the schedule for you now. Water shut-off times are usually managed by local "
    "utilities, so I'll check the public web to see if I can find that information for your area. 😊",
    "I am looking into that information for you. I will let you know as soon as I have it.",
    "Sure, let me check that.", "One moment!", "I’ll find that for you."])
def test_promises_are_detected(text):
    assert mo.promise_only_reply(text)


@pytest.mark.parametrize("text", [
    "", "RAM is your computer's short-term memory: it holds what running apps are using.",
    "Let me know if you'd like more detail.",
    "Which city is the shutoff in? Once I have that, I'll check the utility's notices.",
    "RAM is short-term memory. " * 20 + "I'll check back if you want more.",
])
def test_answers_and_questions_are_not_promises(text):
    assert not mo.promise_only_reply(text)


def test_a_stall_is_nudged_once_and_the_next_reply_is_the_answer(synthetic):
    effects, _ = synthetic
    client, events, answer = run(
        [RAM_STALL, [call("get_upcoming", days=1)], "RAM holds what running apps are using."],
        prompt="what is RAM for?", ask_quality=True, model_led_preload=["get_upcoming"])
    assert nudged(client.requests[1])
    assert client.requests[1]["messages"][-2] == {"role": "assistant", "content": RAM_STALL}
    assert client.requests[1]["tool_choice"] == "auto"  # call the tool OR answer
    assert executed_names(effects) == ["get_upcoming"]
    assert answer == "RAM holds what running apps are using."
    assert RAM_STALL not in [e.get("text") for e in events if e["type"] == "text"]


def test_the_nudged_reply_may_answer_without_a_tool(synthetic):
    client, _, answer = run(
        [RAM_STALL, "RAM is short-term memory for running apps."],
        prompt="what is RAM for?", ask_quality=True, model_led_preload=["get_upcoming"])
    assert len(client.requests) == 2 and nudged(client.requests[1])
    assert answer == "RAM is short-term memory for running apps."


def test_one_retry_only_and_the_second_reply_is_kept_unedited(synthetic):
    effects, _ = synthetic
    second = "I'll look into RAM for you right away."
    client, events, answer = run(
        [RAM_STALL, second, "never reached"],
        prompt="what is RAM for?", ask_quality=True, model_led_preload=["get_upcoming"])
    assert len(client.requests) == 2
    assert not effects
    assert answer == second  # accepted as the model wrote it; never rewritten


def test_a_clarifying_question_is_not_nudged(synthetic):
    question = "Which city is the water shut-off in? Once I know, I'll check the utility's notices."
    client, _, answer = run([question, "never reached"], prompt="when is the water shut off?",
                            ask_quality=True, model_led_preload=["get_upcoming"])
    assert len(client.requests) == 1 and answer == question


def test_a_chat_turn_with_no_tools_offered_is_not_nudged(synthetic):
    client, _, answer = run(["Let me take a look at how you're feeling: I'm glad you said hi!", "never reached"],
                            prompt="hi, how are you?", ask_quality=True, ask_direct=True,
                            model_led_preload=["get_upcoming"])
    assert len(client.requests) == 1
    assert not client.requests[0].get("tools")
    assert "never reached" not in answer


def test_a_writing_turn_is_not_nudged(synthetic):
    reply = "Let me take a look at autumn: leaves fall, slow and gold."
    client, _, answer = run([reply, "never reached"], prompt="write a short poem about autumn",
                            ask_quality=True, model_led_preload=["get_upcoming"])
    assert len(client.requests) == 1 and answer == reply


def test_the_last_allowed_step_is_not_nudged(synthetic):
    client, _, answer = run([RAM_STALL, "never reached"], prompt="what is RAM for?", ask_quality=True,
                            model_led_preload=["get_upcoming"], max_steps=1)
    assert len(client.requests) == 1
    assert not any(nudged(r) for r in client.requests)


def test_a_promise_after_a_tool_ran_is_not_nudged(synthetic):
    client, _, answer = run(
        [[call("get_upcoming", days=1)], "Workshop tomorrow at 14:00. I'll check again later if you like."],
        prompt="what is up tomorrow", ask_quality=True, model_led_preload=["get_upcoming"])
    assert len(client.requests) == 2
    assert not any(nudged(r) for r in client.requests)


def executed_names(effects):
    return [name for name, _ in effects]
