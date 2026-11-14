"""Ask and Wisp's own permissions, switches and "Let Wisp act for me".

From a run of judge-style prompts in a 0.5.0 preview build (2026-11-13):
- "Turn on Full access and always allow sending email" was answered as done.
  No tool ran, and Ask has no tool that changes a permission or a switch.
- Ask could not say what Wisp does on its own, and "Will you reply to my
  emails for me?" ended in "Which source?".

A request to change one of Wisp's own permissions or switches gets one fixed,
true answer before any model step; a reply that claims such a change is
replaced by it; and the questions about acting, replying, privacy and what's
new are answered by Wisp's own report, written from acting.py's wording and
live state.

Scripted model clients and synthetic tools through the real loop. No model,
server lifespan, native tool, network or user data.
"""
import pytest

from service import model_only as mo
from service import wisp_about
from service.proactive import acting
from tests.test_model_led_integration import Approval, call, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

CLAIM = "Done! I've turned on Full access and set email sending to always allow."


def ask(script, prompt, **kwargs):
    return run(script, prompt=prompt, ask_quality=True, **kwargs)


def published(events):
    return [e.get("text") for e in events if e["type"] == "text"]


REQUESTS = [
    "Turn on Full access and always allow sending email",
    "turn full access on",
    "Can you turn on Full access?",
    "Enable Demo mode",
    "How do I turn on Demo mode?",
    "switch off demo mode",
    "go into view only mode",
    "Let Wisp act for me",
    "yes, act for me",
    "Stop asking me before sending",
    "always allow emails to Dana",
    "Don't ask me again",
    "From now on send my texts without asking",
    "never ask me for confirmation",
    "Give yourself full access",
]
NOT_REQUESTS = [
    "Is Full access on?",
    "What does Demo mode do?",
    "Give Dana full access to the folder",
    "Text mom I'm running late",
    "Stop asking me about my day",
    "Remind me to call the dentist tomorrow at 9am",
    "turn on do not disturb",
    "Share the doc as view only",
    "Don't ask Dana about the trip",
    "What's on my calendar tomorrow?",
]


@pytest.mark.parametrize("prompt", REQUESTS)
def test_a_request_to_change_wisps_own_switch_gets_the_fixed_answer_and_no_model_step(synthetic, prompt):
    effects, register = synthetic
    register("toggle_setting", "system_tools", "system_write")
    client, events, answer = ask([[call("toggle_setting")], CLAIM], prompt)
    assert client.requests == [] and not effects
    assert answer == mo.OWN_SWITCH_ANSWER and published(events) == [mo.OWN_SWITCH_ANSWER]
    assert events[-1]["type"] == "done"


@pytest.mark.parametrize("prompt", NOT_REQUESTS)
def test_other_requests_and_questions_about_a_switch_still_reach_the_model(prompt):
    assert not mo.own_switch_request(prompt)


def test_the_fixed_answer_says_where_and_that_sends_always_ask():
    text = mo.OWN_SWITCH_ANSWER
    assert "can't change that myself" in text and "Nothing was changed" in text
    for place in ("Settings › Privacy", "notch's settings", "menu-bar icon's menu"):
        assert place in text
    assert "every email and text asks you every time" in text
    assert "can't be set to always allow" in text
    assert not mo.own_switch_claim(text)


def test_writing_about_a_switch_and_a_quoted_request_are_not_caught(synthetic):
    client, _, answer = ask(["Hi team, please enable full access for my account."],
                            "Write an email to IT asking them to enable full access for me")
    assert len(client.requests) == 1 and answer != mo.OWN_SWITCH_ANSWER
    client, _, answer = ask(["Which Dana?"], 'Text Dana "turn on full access"')
    assert len(client.requests) == 1 and answer != mo.OWN_SWITCH_ANSWER


@pytest.mark.parametrize("claim", [
    CLAIM,
    "Full access is now on.",
    "I have enabled Demo mode for you.",
    "Okay, I won't ask again before sending.",
    "I'll send your texts without asking from now on.",
    "Full access has been turned on.",
    "I've switched on acting for you.",
])
def test_a_reply_that_claims_the_change_is_replaced(synthetic, claim):
    # A follow-up the up-front check cannot read ("yes do both") still ends true.
    history = [{"role": "user", "content": "what is full access"},
               {"role": "assistant", "content": "It lets small changes run without a card. Want it on?"}]
    client, events, answer = ask([claim], "yes do both", history=history)
    assert len(client.requests) >= 1
    assert answer == mo.OWN_SWITCH_ANSWER and published(events)[-1] == mo.OWN_SWITCH_ANSWER


@pytest.mark.parametrize("reply", [
    "I can't turn on Full access myself.",
    "Full access is on.",
    "You can turn on Demo mode in the menu-bar icon's menu.",
    "I've set a reminder for 9 AM.",
    "Full access lets me add reminders without asking.",
])
def test_true_statements_about_a_switch_are_not_claims(reply):
    assert not mo.own_switch_claim(reply)


QUESTIONS = {
    "How much do you do on your own?": "acting",
    "Do you do anything about my deadlines on your own?": "acting",
    "What does Let Wisp act for me do?": "acting",
    "Will you reply to my emails for me?": "replies",
    "Does anything I type leave this Mac?": "leaves",
    "What's new in Wisp?": "whats_new",
}


@pytest.mark.parametrize("prompt,kind", QUESTIONS.items())
def test_questions_about_wisp_acting_are_answered_by_its_own_report(synthetic, prompt, kind):
    effects, register = synthetic
    register("view_emails", "mail_tools")
    client, events, answer = ask([[call("view_emails")], "Which source?"], prompt)
    assert mo.own_question(prompt) == kind
    assert client.requests == [] and not effects          # no model step, no dead end
    assert answer == wisp_about.own_answer(kind) and published(events) == [answer]
    assert "Which source" not in answer
    assert "|" not in answer and "- **" in answer         # a list with bold labels, no table


@pytest.mark.parametrize("prompt", [
    "Can you reply to Dana's email for me?", "What's new in my inbox?", "Reply to my emails",
    "What can you do?", "Remind me to leave the Mac at home", "What's on my calendar tomorrow?",
])
def test_requests_and_other_questions_are_not_taken(prompt):
    assert mo.own_question(prompt) == ""


def test_acting_answer_while_off_names_the_switch_no_daily_limit_and_never_sends(monkeypatch):
    monkeypatch.setattr(acting, "enabled", lambda action=None: False)
    text = wisp_about.acting()
    assert '"Let Wisp act for me" is off' in text and "until you say yes" in text
    assert "none per day" in text and "at most" not in text     # the daily cap was removed
    assert acting.NEVER in text and "I draft replies for you to send" in text
    assert acting.SCOPE in text and "Settings › Privacy" in text
    for line in acting.DOES:
        assert line in text                               # acting.py's own wording
    assert "green-bubble" in text and "used" not in text


def test_acting_answer_while_on_lists_only_agreed_actions_and_todays_count(monkeypatch):
    monkeypatch.setattr(wisp_about, "_acting_state", lambda: {
        "enabled": True, "never": acting.NEVER, "scope": acting.SCOPE,
        "actions": dict(acting.ACTIONS), "covered": ["add", "hold"], "used": 3})
    text = wisp_about.acting()
    assert '"Let Wisp act for me" is on' in text
    assert acting.ACTIONS["add"] in text and acting.ACTIONS["hold"] in text
    assert acting.ACTIONS["follow_up"] not in text and "Not agreed yet" in text
    assert "none per day" in text and "3 done in the last day" in text and acting.NEVER in text


def test_acting_answer_when_state_cannot_be_read_claims_nothing(monkeypatch):
    monkeypatch.setattr(wisp_about, "_acting_state", lambda: None)
    text = wisp_about.acting()
    assert "couldn't check" in text and "never sends" in text
    assert "No. I never send a reply by myself." in wisp_about.replies()


def test_replies_leaves_and_whats_new_carry_their_facts(monkeypatch):
    monkeypatch.setattr(acting, "enabled", lambda action=None: False)
    replies = wisp_about.replies()
    assert replies.startswith("**No. I never send a reply by myself.**")
    assert "Drafts" in replies and acting.NEVER in replies and "It is off now." in replies
    leaves = wisp_about.leaves()
    assert "a web lookup sends only its search words" in leaves
    assert leaves.splitlines()[0] == wisp_about.privacy().splitlines()[0]   # the live local/cloud line
    new = wisp_about.whats_new()
    for label in ("Acting for you", "Gemma Wisp", "Demo mode", "Web privacy"):
        assert f"**{label}**" in new
    assert "Wisp never sends anything by itself" in new and "at most" not in new


def test_the_self_hint_carries_the_acting_facts_for_other_wordings():
    assert mo.wisp_self_question("How much do you do on your own?")
    hint = mo.wisp_self_hint()
    assert acting.NEVER in hint and "with no limit per day" in hint and "at most" not in hint
    assert "cannot change your own permissions" in hint
