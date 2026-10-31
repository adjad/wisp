"""The final Ask answer is checked against this turn's tool results
(service/agent/grounding.py): a value nothing showed, or a "done"/"checked"
claim with nothing run, earns ONE re-prompt; clean answers, general
knowledge, calculations, chat and restated relative dates pass untouched.
Toolbench 2026-10-31: 175 ungrounded_answer, plus no_call answers such as
"I've set a timer" with no tool run."""
from __future__ import annotations

import datetime as dt
import os
import tempfile

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-grounding-"))

import pytest  # noqa: E402

from service.agent.grounding import check, unbacked_claim, unmatched_values  # noqa: E402

TODAY = dt.date(2026, 10, 31)
SYSTEM = "Today is Saturday, October 31, 2026. The user is Tess Rivera."
UPCOMING = ("Tomorrow (2026-11-01):\n- 11:00 AM Brunch with Sam at Nopa\n"
            "- 15:30–16:30 Dentist (Dr. Patel)\nNov 3: Budget review with vendor")
MAIL = ('From: Chase <alerts@chase.com>\nSubject: Your statement is ready\n'
        'Message-ID: <a1@chase>\nYour balance is 1,284.17. Minimum payment 35.00.')


def turn(result, prompt="what's on tomorrow?", calls=None):
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "get_upcoming", "arguments": calls or "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": result}]


def grounded(answer, msgs, **kw):
    kw = {"ran_tools": True, "had_results": True, "asks_action": False, "asks_personal": True,
          "today": TODAY, "own_names": ["Tess Rivera", "Tess", "Rivera"], **kw}
    return check(answer, msgs, **kw)


# ---- hits ----------------------------------------------------------------
@pytest.mark.parametrize("answer, value", [
    ("You have brunch with Sam at 10:00 AM tomorrow.", "10:00 AM"),
    ("Your dentist is at 4:15 PM.", "4:15 PM"),
    ("Brunch is with Jordan at Nopa.", "Jordan"),
    ("Your budget review is on November 19.", "November 19"),
    ("Your Chase balance is $1,482.17.", "$1,482.17"),
    ("It came from billing@chase.com.", "billing@chase.com"),
    ('The event is called "Quarterly planning sync".', '"Quarterly planning sync"'),
])
def test_a_value_no_result_shows_is_flagged(answer, value):
    result = grounded(answer, turn(UPCOMING + "\n" + MAIL))
    assert value in result and "do not guess" in result


@pytest.mark.parametrize("answer", [
    "I've set a timer for ten minutes for you.",
    "Thank you! I've updated Mom's contact details to include mom@example.com.",
    "I've checked, and I can confirm a reminder is set for 6:00 PM.",
    "I will start by running the organization process on your Downloads folder.",
])
def test_a_done_or_checked_claim_with_nothing_run_is_flagged(answer):
    result = check(answer, turn("")[:2], ran_tools=False, had_results=False,
                   asks_action=True, asks_personal=False)
    assert "no tool has run on this turn" in result


# ---- passes --------------------------------------------------------------
@pytest.mark.parametrize("answer", [
    "Tomorrow you have brunch with Sam at 11:00 AM at Nopa, then the dentist with Dr. Patel at 3:30 PM.",
    "Tomorrow, Sunday, November 1st: brunch at 11 AM and the dentist at 3:30–4:30 PM.",
    "On Tuesday, November 3, there's a budget review with the vendor.",
    "Your Chase balance is $1,284.17, and the minimum payment is $35.00.",
    "After the minimum payment you'd owe $1,249.17.",            # a difference of two amounts
    "Sounds good, Tess. Brunch with Sam is the only thing before noon.",
    'The statement email "Your statement is ready" came from alerts@chase.com.',
    "Nothing is on for Friday, November 6.",                     # a weekday date this week
])
def test_answers_the_results_support_pass(answer):
    assert grounded(answer, turn(UPCOMING + "\n" + MAIL)) == ""


@pytest.mark.parametrize("answer", [
    "180 lbs is approximately 81.65 kg.",                       # a calculation
    "The Eiffel Tower opened on March 31, 1889, at about 2:35 PM.",  # general knowledge
    "Happy to chat! What's on your mind?",                      # plain chat
])
def test_no_results_means_no_value_check(answer):
    assert check(answer, turn("")[:2], ran_tools=False, had_results=False,
                 asks_action=False, asks_personal=False, today=TODAY) == ""


def test_the_users_own_name_and_the_prompt_are_evidence():
    msgs = turn("Reminder added: Call the vet, Fri at 5", prompt="remind me to call the vet friday at 5")
    assert grounded("Done, Tess: I'll remind you to call the vet on Friday at 5:00 PM.", msgs) == ""


def test_a_drafted_text_from_the_models_own_call_is_evidence():
    msgs = turn("Sent.", prompt="text Sam that I'm running late",
                calls='{"to": "Sam", "text": "Running about ten minutes late, sorry!"}')
    assert grounded('I texted Sam: "Running about ten minutes late, sorry!"', msgs) == ""


def test_a_question_back_is_not_a_promise():
    assert unbacked_claim("I'll set that reminder, what time on Friday works?") == ""
    assert unbacked_claim("I can set that for you once you tell me the time.") == ""


def test_a_claim_after_a_tool_ran_is_left_to_the_effect_receipts():
    assert grounded("I've set a timer for ten minutes.", turn("Timer set for 10 minutes."),
                    asks_action=True) == ""


def test_chat_with_no_action_or_personal_ask_is_not_checked_for_claims():
    # "thanks!" after an earlier send: no tool ran THIS turn, and that is fine.
    assert check("You're welcome! I've sent it already.", turn("")[:2], ran_tools=False,
                 had_results=False, asks_action=False, asks_personal=False) == ""


def test_at_most_four_values_are_listed():
    answer = "At 1:05 PM, 2:05 PM, 3:05 PM, 4:05 PM and 5:05 PM."
    assert len(unmatched_values(answer, UPCOMING, today=TODAY)) == 4


# ---- in the loop -----------------------------------------------------------
def test_the_loop_reprompts_once_and_accepts_the_next_answer(monkeypatch):
    from tests.test_model_led_integration import call, run
    from tests.test_model_led_integration import synthetic as _synthetic
    effects, register = _synthetic.__wrapped__(monkeypatch)
    tool = register("get_upcoming", "assistant_tools", "assistant_read")
    async def upcoming(**args):
        return UPCOMING
    upcoming.__module__ = tool.func.__module__
    from dataclasses import replace
    from service.tools import registry
    registry.REGISTRY["get_upcoming"] = replace(tool, func=upcoming)
    client, events, answer = run(
        [[call("get_upcoming")], "Brunch with Jordan at 10:00 AM.", "Brunch with Jordan at 10:00 AM."],
        prompt="what's on tomorrow?", ask_quality=True, model_led_preload=["get_upcoming"])
    corrections = [m["content"] for req in client.requests for m in req["messages"]
                   if m.get("role") == "user" and "Check your reply" in str(m.get("content"))]
    assert len(client.requests) == 3                      # tool step, answer, ONE re-prompt
    assert corrections and "10:00 AM" in corrections[-1] and "Jordan" in corrections[-1]
    assert answer.strip() == "Brunch with Jordan at 10:00 AM."   # accepted as it came, never edited




def test_the_loop_adds_no_model_call_for_a_clean_answer(monkeypatch):
    from tests.test_model_led_integration import call, run
    from tests.test_model_led_integration import synthetic as _synthetic
    effects, register = _synthetic.__wrapped__(monkeypatch)
    tool = register("get_upcoming", "assistant_tools", "assistant_read")
    async def upcoming(**args):
        return UPCOMING
    upcoming.__module__ = tool.func.__module__
    from dataclasses import replace
    from service.tools import registry
    registry.REGISTRY["get_upcoming"] = replace(tool, func=upcoming)
    client, _, answer = run([[call("get_upcoming")], "Brunch with Sam at 11:00 AM at Nopa."],
                            prompt="what's on tomorrow?", ask_quality=True,
                            model_led_preload=["get_upcoming"])
    assert len(client.requests) == 2
    assert "Brunch with Sam" in answer
