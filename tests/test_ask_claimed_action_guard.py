"""Ask actions claimed with no tool call, and what a bare "yes" can reach.

From a 2026-11-04 Ask session (described here in synthetic words only):
- The assistant offered "Should I set a reminder for any of those?". The user
  said "yes go ahead". Every write was forbidden for that bare assent, so the
  reminder could not be made, and the reply claimed "Set a reminder tomorrow at
  10 PM…" with no tool call. The user looked for a reminder that never existed.
- "can you text mom this" ended on a draft card with no prose. It was stored as
  an empty reply, so the next turn saw an unanswered request.

Scripted model clients, synthetic tools and a scratch session store only. No
model, server lifespan, native tool, network or user data.
"""
import copy
from pathlib import Path

import pytest

from service import model_only as mo
from service.agent import loop
from service.router import model_led as ml
from service.tools import registry
from tests.test_model_led_integration import Approval, call, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

OFFER = ("Tomorrow you have a study group at 10 AM and the lab report is due at 11:59 PM. "
         "Should I set a reminder for any of those?")
CLAIM = "Set a reminder tomorrow at 9 PM: finish the lab report. That leaves time to review it."
NUDGE = "Nothing has run yet, so that was not done."


@pytest.fixture(autouse=True)
def _model_only(monkeypatch):
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    monkeypatch.delenv("WISP_MODEL_ONLY_NARRATION_THINKING", raising=False)
    monkeypatch.setattr(loop, "audit", lambda *a, **k: None)


def _reminder_tool(register):
    register("add_reminder", "assistant_tools", "assistant_write", parameters={
        "type": "object", "properties": {"title": {"type": "string"}, "due": {"type": "string"}},
        "required": ["title"], "additionalProperties": False})


OWNER = {"claim_effect": lambda tool, args: {"admitted": True},
         "finish_effect": lambda claim, ok: True}


def nudged(request):
    last = request["messages"][-1]
    return last["role"] == "user" and NUDGE in last["content"]


def history(offer=OFFER):
    return ({"role": "user", "content": "how should i spend tomorrow"},
            {"role": "assistant", "content": offer})


# --- the claim detector ---------------------------------------------------------

@pytest.mark.parametrize("text,first_person", [
    (CLAIM, False),
    ("I've set a reminder so you can finish the lab report.", True),
    ("Done! Your reminder is set for 9 PM.", False),
    ("Reminder set for Friday at 5 PM.", False),
    ("Here is your day. I've added the dentist to your calendar.", True),
    ("I sent it.", True),
    ("All set!", False),
    ("Okay — I've texted Sam that you're running late.", True),
    ("I turned on Do Not Disturb.", True),
])
def test_completed_changes_are_claims(text, first_person):
    claim = mo.claimed_effect(text)
    assert claim is not None and claim.first_person is first_person


@pytest.mark.parametrize("text", [
    "I can set a reminder for that.",
    "Should I set a reminder for any of those?",
    "Would you like me to add it to your calendar?",
    "I'll set a reminder now.",
    "I didn't set it, so nothing has changed.",
    "Okay — I didn't send it.",
    "I've put together your plan for today.",
    "I added up your meetings: 3 hours in all.",
    "I've set aside the afternoon for the lab report in this plan.",
    "You have a study group at 10 AM.\n- Sent: lab report draft (email from Sam)",
    "If you want, I added notes.",
    "Nothing was found for that reminder.",
    "The sun shines because of fusion in its core.",
    "",
])
def test_offers_questions_negations_and_narration_are_not_claims(text):
    assert mo.claimed_effect(text) is None


def test_a_back_reference_is_marked():
    assert mo.claimed_effect("I set that reminder earlier today.").backref
    assert not mo.claimed_effect("I set that reminder.").backref


# --- what a bare assent may reach -------------------------------------------------

@pytest.mark.parametrize("prompt,last,expected", [
    ("yes go ahead", OFFER, {"add_reminder"}),
    ("yes", "I couldn't find a matching reminder for that. Would you like me to set it anyway?",
     {"add_reminder"}),
    ("sure", "Want me to remind you at 8?", {"add_reminder"}),
    ("ok", "Should I start a 20 minute timer?", {"set_timer"}),
    ("yes please", "Do you want me to add it to your calendar?", {"add_calendar_event"}),
    ("yep", "Shall I save that as a note?", {"create_note"}),
])
def test_assent_to_a_create_offer_reaches_only_that_creation(prompt, last, expected):
    assert ml.continuation_create_exemption(prompt, last) == expected
    assert ml.answers_offer(prompt, last)


@pytest.mark.parametrize("prompt,last", [
    ("no", OFFER),                                                     # a decline
    ("never mind", OFFER),
    ("yes", "Your study group is at 10 AM."),                          # no offer
    ("yes", "I can also set reminders. What time works for the group?"),  # aside, not the closing offer
    ("yes", "Should I delete the old reminders?"),                     # not a creation
    ("yes", "Want me to move the reminder to Friday?"),
    ("set one for 9 pm", OFFER),                                       # not a bare assent: normal menu
    ("thanks", OFFER),                                                 # closes, accepts nothing
    ("ok", 'The email says: "Want me to set a timer?" Anyway, your day is open.'),  # quoted, not closing
])
def test_no_create_exemption_without_an_assent_to_a_create_offer(prompt, last):
    assert ml.continuation_create_exemption(prompt, last) == frozenset()


# --- the loop guard -------------------------------------------------------------

def test_an_unbacked_claim_on_an_accepted_offer_is_nudged_and_then_runs(synthetic):
    effects, register = synthetic
    _reminder_tool(register)
    client, events, answer = run(
        [CLAIM, [call("add_reminder", title="Finish the lab report", due="tomorrow 9 PM")],
         "Done: the reminder is in."],
        prompt="yes go ahead", history=history(), ask_quality=True,
        model_led_preload=["add_reminder"], approval=Approval(allowed=True), **OWNER)
    assert nudged(client.requests[1])
    assert client.requests[1]["tool_choice"] == "auto"
    assert client.requests[1]["messages"][-2] == {"role": "assistant", "content": CLAIM}
    assert [name for name, _ in effects] == ["add_reminder"]
    assert CLAIM not in [e.get("text") for e in events if e["type"] == "text"]


def test_a_claim_that_survives_the_nudge_becomes_a_plain_not_done(synthetic):
    effects, register = synthetic
    _reminder_tool(register)
    client, events, answer = run(
        [CLAIM, "Reminder set for 9 PM tomorrow.", "never reached"],
        prompt="yes go ahead", history=history(), ask_quality=True,
        model_led_preload=["add_reminder"])
    assert len(client.requests) == 2 and nudged(client.requests[1])
    assert not effects
    assert answer == mo.ASK_CLAIM_NOT_DONE
    published = [e.get("text") for e in events if e["type"] == "text"]
    assert published == [mo.ASK_CLAIM_NOT_DONE]


def test_a_first_person_claim_with_no_change_asked_is_corrected_not_nudged(synthetic):
    effects, register = synthetic
    _reminder_tool(register)
    client, _, answer = run(
        ["I've set a reminder for your study group.", "never reached"],
        prompt="ok thanks", ask_quality=True, model_led_preload=["get_upcoming"])
    assert len(client.requests) == 1  # never turned into a call the user did not ask for
    assert not effects and answer == mo.ASK_CLAIM_NOT_DONE


@pytest.mark.parametrize("reply", [
    "I've saved you a trip: the store closes at 5.",
    "I've updated my summary based on your calendar reads.",
    "I removed the duplicates from the list above.",
])
def test_first_person_phrasing_on_a_read_only_turn_with_no_item_stands(synthetic, reply):
    _, register = synthetic
    _reminder_tool(register)
    client, _, answer = run([reply, "never reached"], prompt="ok thanks", ask_quality=True,
                            model_led_preload=["get_upcoming"])
    assert len(client.requests) == 1 and answer == reply


def test_subjectless_advice_with_no_change_asked_stands(synthetic):
    _, register = synthetic
    _reminder_tool(register)
    advice = "Set a reminder for 6 PM so the call doesn't slip, and keep the evening light."
    client, _, answer = run([advice, "never reached"], prompt="how do i remember to call sam",
                            ask_quality=True, model_led_preload=["get_upcoming"])
    assert len(client.requests) == 1 and answer == advice


@pytest.mark.parametrize("reply", [
    "I can set a reminder for 9 PM tomorrow. Want me to?",
    "Which one should I set a reminder for, the study group or the lab report?",
])
def test_offers_and_questions_are_not_nudged(synthetic, reply):
    _, register = synthetic
    _reminder_tool(register)
    client, _, answer = run([reply, "never reached"], prompt="yes go ahead", history=history(),
                            ask_quality=True, model_led_preload=["add_reminder"])
    assert len(client.requests) == 1 and answer == reply


def test_a_claim_after_the_write_ran_is_left_to_the_receipt_check(synthetic):
    effects, register = synthetic
    _reminder_tool(register)
    client, _, _ = run(
        [[call("add_reminder", title="Finish the lab report")], "I've set the reminder."],
        prompt="yes go ahead", history=history(), ask_quality=True,
        model_led_preload=["add_reminder"], approval=Approval(allowed=True), **OWNER)
    assert [name for name, _ in effects] == ["add_reminder"]
    assert not any(nudged(r) for r in client.requests)


def test_an_earlier_real_write_lets_a_back_reference_stand(synthetic):
    _, register = synthetic
    _reminder_tool(register)
    reply = "I set that reminder earlier, for 9 PM tomorrow."
    client, _, answer = run([reply, "never reached"], prompt="did you set it?",
                            history=({"role": "user", "content": "remind me at 9"},
                                     {"role": "assistant", "content": "Reminder added for 9 PM."}),
                            ask_quality=True, model_led_preload=["get_upcoming"], earlier_effects=True)
    assert len(client.requests) == 1 and answer == reply


def test_without_an_earlier_write_the_same_back_reference_is_corrected(synthetic):
    _, register = synthetic
    _reminder_tool(register)
    client, _, answer = run(["I set that reminder earlier, for 9 PM tomorrow.", "never reached"],
                            prompt="did you set it?", ask_quality=True,
                            model_led_preload=["get_upcoming"])
    assert answer == mo.ASK_CLAIM_NOT_DONE


def test_the_guard_never_runs_off_the_ask_path(synthetic):
    _, register = synthetic
    _reminder_tool(register)
    client, _, answer = run([CLAIM, "never reached"], prompt="yes go ahead", history=history(),
                            model_led_preload=["add_reminder"])
    assert len(client.requests) == 1 and answer == CLAIM


# --- the /agent entrypoint: the assent's menu, and a draft turn's stored reply ---

from tests.test_model_led_entrypoint import endpoint  # noqa: E402,F401  (fixture)


def _entry_tools(endpoint):
    async def fake(**args):
        return "Synthetic reminder result."
    fake.__module__ = "service.tools.assistant_tools"
    for name in ("add_reminder", "update_reminder", "complete_reminder"):
        registry.REGISTRY[name] = registry.Tool(name, "Synthetic reminder write",
            {"type": "object", "properties": {"title": {"type": "string"}}}, "assistant_write", fake)


def test_yes_to_a_reminder_offer_keeps_only_that_creation_and_leads_with_it(endpoint):
    _entry_tools(endpoint)
    sid = endpoint.store.create_session()
    endpoint.store.add_turn(sid, "user", "how should i spend tomorrow")
    endpoint.store.add_turn(sid, "assistant", OFFER, tool_digest="get_my_day_plan")
    endpoint.request("yes go ahead", sid)
    call_kwargs = endpoint.agent_calls[0]
    assert "add_reminder" not in call_kwargs["forbidden_tools"]
    assert {"update_reminder", "complete_reminder"} <= set(call_kwargs["forbidden_tools"])
    assert call_kwargs["model_led_preload"][0] == "add_reminder"
    assert call_kwargs["earlier_effects"] is False


def test_yes_with_no_offer_still_forbids_every_write(endpoint):
    _entry_tools(endpoint)
    sid = endpoint.store.create_session()
    endpoint.store.add_turn(sid, "user", "what is up tomorrow")
    endpoint.store.add_turn(sid, "assistant", "A study group at 10 AM.", tool_digest="get_upcoming")
    endpoint.request("yes", sid)
    assert {"add_reminder", "update_reminder"} <= set(endpoint.agent_calls[0]["forbidden_tools"])


def test_an_earlier_write_in_the_chat_reaches_the_loop(endpoint):
    _entry_tools(endpoint)
    sid = endpoint.store.create_session()
    endpoint.store.add_turn(sid, "user", "remind me at 9")
    endpoint.store.add_turn(sid, "assistant", "Reminder added.", tool_digest="add_reminder")
    endpoint.request("did you set it?", sid)
    assert endpoint.agent_calls[0]["earlier_effects"] is True


def test_a_draft_turn_is_stored_as_a_note_not_an_empty_reply(endpoint, monkeypatch):
    from service import main

    async def drafted(client, model, messages, emit, approver, **kwargs):
        endpoint.agent_calls.append(copy.deepcopy({"messages": messages, **kwargs}))
        await emit({"type": "tool_call", "id": "d1", "name": "draft_message", "args": {}})
        await emit({"type": "message_draft", "to": "Sam", "text": "Running ten minutes late."})
        await emit({"type": "done"})
        return ""
    monkeypatch.setattr(main, "run_agent", drafted)
    sid = endpoint.store.create_session()
    endpoint.request("can you text sam this", sid)
    stored = [t for t in endpoint.store.turns_from(sid, 0) if t["role"] == "assistant"]
    assert len(stored) == 1
    note = stored[0]["content"]
    assert note.startswith("Drafted a text to Sam for you to review:")
    assert "Running ten minutes late." in note and "sent nothing" in note
    assert stored[0]["tool_digest"] == "draft_message"


def test_the_notch_keeps_a_transcript_line_for_a_draft_turn():
    # OverlayModel has no Swift unit harness; the `done` handler must append an
    # assistant turn when the reply was only a draft card.
    source = (Path(__file__).resolve().parents[1]
              / "app/Sources/WispApp/OverlayModel.swift").read_text()
    done = source[source.index('case "done":'):]
    done = done[:done.index("default: break")]
    assert "} else if let draft = messageDraft {" in done
    assert "Drafted a text to" in done and "turns.append(t)" in done
