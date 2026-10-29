"""Tool-call markup written as the answer.

The 2026-10-29 Ask run (1,040 questions on the user's Mac) showed 13 of the
first 252 answers being nothing but Gemma's call markup, e.g.
`<|tool_call>call:public_web:web_search{query:<|"|>what is a hash table?<|"|>}<tool_call|>`.
Almost all were knowledge questions, where Ask offers no tools; the model wrote
a call anyway (often with a made-up namespaced name) and the text reached the
user. The markup is never shown: a knowledge question gets one nudge to answer
in plain text, a personal-data question gets the "call the tool now" re-prompt.
"""
from __future__ import annotations

import pytest

from service import model_only as mo
from tests.test_model_led_integration import call, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

LEAKS = [
    '<|tool_call>call:public_web:web_search{query:<|"|>what is a hash table?<|"|>}<tool_call|>',
    '<|tool_call>call: device.system_status {section: "disk"}',
    '<|tool_call>call: compute: calculate\nfunction: calculate\nargs:\n  number: 144\n---',
]


@pytest.mark.parametrize("text", LEAKS)
def test_markup_is_recognised_and_stripped_whole(text):
    assert mo.text_tool_call(text)
    assert mo.strip_text_tool_calls(text) == ""


def test_an_answer_around_markup_keeps_only_the_answer():
    text = 'A hash table maps keys to slots. <|tool_call>call:web_search{q:<|"|>x<|"|>}<tool_call|>'
    assert mo.strip_text_tool_calls(text) == "A hash table maps keys to slots."
    assert not mo.text_tool_call("Plain answer with a <b>tag</b>.")


def test_a_knowledge_question_gets_one_nudge_and_a_plain_answer(synthetic):
    client, events, answer = run(
        [LEAKS[0], "A hash table stores values in slots picked by hashing their keys."],
        prompt="what is a hash table?", ask_quality=True, model_led_preload=["get_upcoming"])
    assert answer == "A hash table stores values in slots picked by hashing their keys."
    assert len(client.requests) == 2
    assert "plain text" in client.requests[1]["messages"][-1]["content"]
    assert not any("<|tool_call>" in (e.get("text") or "") for e in events)


def test_markup_twice_never_reaches_the_user(synthetic):
    _, events, answer = run([LEAKS[0], LEAKS[0], LEAKS[0]], prompt="what is a hash table?",
                            ask_quality=True, model_led_preload=["get_upcoming"])
    assert "<|tool_call>" not in answer
    assert not any("<|tool_call>" in (e.get("text") or "") for e in events)


def test_a_personal_question_written_as_markup_is_reprompted_to_call_the_tool(synthetic):
    effects, _ = synthetic
    client, _, answer = run(
        ['<|tool_call>call: calendar:get_upcoming{period:<|"|>tomorrow<|"|>}<tool_call|>',
         [call("get_upcoming", period="tomorrow")], "Tomorrow: Office Hours 10–11 AM."],
        prompt="what do I have tomorrow?", ask_quality=True, model_led_preload=["get_upcoming"])
    assert client.requests[1]["tool_choice"] == "required"
    assert answer == "Tomorrow: Office Hours 10–11 AM."


@pytest.mark.parametrize("ro,full", [(True, False), (False, False), (False, True)])
def test_a_calculation_runs_in_every_access_mode(monkeypatch, ro, full):
    """"convert 72 F to C" was denied in view-only and would have asked for a
    confirmation in the default mode: compute fell through to "unclassified"."""
    from service.safety import policy
    from service.tools.registry import REGISTRY
    monkeypatch.setattr(policy, "_READ_ONLY", ro)
    monkeypatch.setattr(policy, "_FULL_ACCESS", full)
    for name, args in (("convert_units", {"value": 72, "from_unit": "F", "to_unit": "C"}),
                       ("calculate", {"expression": "23*47"}), ("world_time", {"place": "Tokyo"})):
        assert REGISTRY[name].category == "compute"
        assert policy.decide("compute", args, tool=name).tier is policy.Tier.ALLOW
    # Anything still unclassified stays conservative.
    assert policy.decide("no_such_category", {}).tier is not policy.Tier.ALLOW or full
