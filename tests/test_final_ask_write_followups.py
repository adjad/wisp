"""Ask writes that were claimed, misrouted or printed as text.

From a run in a 0.5.0 preview build (2026-11-13), described in synthetic words:
- "Add one more bullet to the Wisp test note" -> "What should it say?" ->
  "Demo point 4" -> the reply said four bullets were saved. Only a note search
  ran; the note was never changed.
- "Delete the Wisp test reminder" got a card for the bulk past-due clearer.
- An answer printed a tool call as text: `get_my_day_plan(day='tomorrow')`.

Scripted model clients and synthetic tools through the real loop. No model,
server lifespan, native tool, network or user data.
"""
import pytest

from service import model_only as mo
from tests.test_model_led_integration import Approval, call, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

ASKED = "Add one more bullet to the Wisp test note"
QUESTION = "Sure. What should the new bullet say?"
ANSWER = "Demo point 4"
CLAIM = "The Wisp test note now has four bullets, and Demo point 4 is saved."
NUDGE = "Nothing has run yet, so that was not done."
OWNER = {"claim_effect": lambda tool, args: {"admitted": True},
         "finish_effect": lambda claim, ok: True}


def history():
    return ({"role": "user", "content": ASKED}, {"role": "assistant", "content": QUESTION})


def note_tools(register):
    register("search_notes", "notes_tools", "notes_read", parameters={
        "type": "object", "properties": {"query": {"type": "string"}}, "additionalProperties": False})
    register("append_note", "notes_write", "notes_write", parameters={
        "type": "object", "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
        "required": ["title", "text"], "additionalProperties": False})


def published(events):
    return [e.get("text") for e in events if e["type"] == "text"]


# --- 1. the claim with no write behind it ---------------------------------------

def test_the_answer_to_wisps_own_question_keeps_the_request_open():
    assert mo.pending_write_request(ANSWER, ASKED, QUESTION) == ASKED
    assert mo.pending_write_request("Buy milk and eggs", ASKED, QUESTION) == ASKED
    for reply in ("no", "never mind", "what's on my calendar?", "delete it instead"):
        assert mo.pending_write_request(reply, ASKED, QUESTION) == ""
    assert mo.pending_write_request(ANSWER, ASKED, "Okay.") == ""                # no question was asked
    assert mo.pending_write_request(ANSWER, "what's in the note", QUESTION) == ""  # nothing to write
    assert ASKED in mo.pending_write_hint(ASKED)


def test_a_saved_claim_after_only_a_search_is_nudged_then_replaced(synthetic):
    effects, register = synthetic
    note_tools(register)
    client, events, answer = run(
        [[call("search_notes", query="Wisp test note")], CLAIM, CLAIM, "never reached"],
        prompt=ANSWER, history=history(), ask_quality=True,
        model_led_preload=["append_note", "search_notes"])
    assert [name for name, _ in effects] == ["search_notes"]
    assert NUDGE in client.requests[2]["messages"][-1]["content"]
    assert answer == mo.ASK_CLAIM_NOT_DONE and published(events)[-1] == mo.ASK_CLAIM_NOT_DONE


def test_the_follow_up_can_make_the_write_and_then_say_so(synthetic):
    effects, register = synthetic
    note_tools(register)
    client, events, answer = run(
        [CLAIM, [call("append_note", title="Wisp test note", text="- Demo point 4")],
         "Added Demo point 4 to the Wisp test note."],
        prompt=ANSWER, history=history(), ask_quality=True, approval=Approval(True),
        model_led_preload=["append_note", "search_notes"], **OWNER)
    assert ("append_note", {"title": "Wisp test note", "text": "- Demo point 4"}) in effects
    # The synthetic tool returns no real receipt, so only the call itself is checked here.
    assert len(client.requests) >= 2 and answer != mo.ASK_CLAIM_NOT_DONE


@pytest.mark.parametrize("text", [
    "The note now has four bullets.",
    "Demo point 4 was added to your Wisp test note.",
    "Your four bullets are saved.",
    "All four bullets have been saved to the note.",
    "I appended Demo point 4 to the note.",
    "Got it. The reminder has been deleted.",
    "I've added Demo point 4 to the Wisp test note.",
    "The email was sent to Dana.",
    "The event is now cancelled.",
])
def test_every_wording_of_done_counts_for_every_write_family(text):
    assert mo.claimed_write(text)


@pytest.mark.parametrize("text", [
    "What should the bullet say?",
    "I can add that to the note.",
    "I couldn't find a note called Wisp test.",
    "The note has three bullets.",
    "Nothing was added.",
    "I'll add it once you tell me the text.",
    "I found the note \"Wisp test note\".",
])
def test_questions_offers_and_plain_reads_are_not_claims(text):
    assert not mo.claimed_write(text)


def test_a_read_turn_that_narrates_a_change_is_left_alone(synthetic):
    effects, register = synthetic
    note_tools(register)
    said = "The Wisp test note has three bullets. It was updated yesterday."
    _, _, answer = run([[call("search_notes", query="Wisp test")], said],
                       prompt="what's in my Wisp test note", ask_quality=True,
                       model_led_preload=["search_notes"])
    assert answer == said


# --- 2. one named reminder ------------------------------------------------------

@pytest.mark.parametrize("prompt,one", [
    ("Delete the Wisp test reminder", True),
    ("delete my dentist reminder", True),
    ("Remove the reminder to call mom", True),
    ("clear my overdue reminders", False),
    ("delete all my reminders", False),
    ("Delete the old reminder about taxes", False),
    ("cancel the reminders for today", False),
    ("delete the Wisp test note", False),
])
def test_one_named_reminder_is_told_from_many(prompt, one):
    assert mo.single_reminder_delete(prompt) is one


def test_a_single_delete_leads_with_cancel_event_and_cannot_reach_a_bulk_clearer(synthetic):
    effects, register = synthetic
    for name in ("cancel_event", "clear_past_reminders", "clear_reminders"):
        register(name, "assistant_tools", "calendar_write", parameters={
            "type": "object", "properties": {"title": {"type": "string"}, "query": {"type": "string"},
                                             "scope": {"type": "string"}}, "additionalProperties": False})
    from service.tools import registry
    prompt = "Delete the Wisp test reminder"
    forbidden = mo.ask_forbidden_tools(prompt, registry.REGISTRY)
    assert forbidden == mo.BULK_REMINDER_CLEARERS
    assert not mo.ask_forbidden_tools("clear my overdue reminders", registry.REGISTRY)
    approval = Approval(True)
    client, events, _ = run(
        [[call("clear_past_reminders", query="Wisp test")], [call("cancel_event", title="Wisp test")],
         "Cancelled it."],
        prompt=prompt, ask_quality=True, approval=approval, forbidden_tools=forbidden,
        model_led_preload=["cancel_event"], **OWNER)
    assert [name for name, _ in effects] == ["cancel_event"]
    assert [a["tool"] for a in approval.calls] == ["cancel_event"]      # the single delete's own card
    offered = {s["function"]["name"] for s in client.requests[0].get("tools") or []}
    assert "cancel_event" in offered and not offered & mo.BULK_REMINDER_CLEARERS
    assert "clear_reminders" not in forbidden      # its card lists exactly the matching reminder


def test_the_real_head_start_puts_the_single_delete_first():
    import service.tools  # noqa: F401  (registers the real tools)
    from service import main
    from service.tools.registry import REGISTRY
    assert main._ask_tool_rank("Delete the Wisp test reminder", "")[0][0] == "cancel_event"
    assert main._ask_tool_rank("clear my overdue reminders", "")[0][0] == "clear_past_reminders"
    assert main._ask_tool_rank(ASKED, "")[0][0] == "append_note"        # what the follow-up now leads with
    assert mo.ask_forbidden_tools("Delete the Wisp test reminder", REGISTRY) == mo.BULK_REMINDER_CLEARERS


# --- 7. a tool call printed in the answer ---------------------------------------

def test_the_exact_leaked_call_is_stripped_from_the_answer(synthetic):
    effects, register = synthetic
    register("get_my_day_plan", "assistant_tools")
    leaked = "Tomorrow you have lunch at noon.\n\n`get_my_day_plan(day='tomorrow')`"
    _, events, answer = run([[call("get_my_day_plan")], leaked], prompt="What's on my calendar tomorrow?", ask_quality=True,
                            model_led_preload=["get_my_day_plan"])
    assert answer == "Tomorrow you have lunch at noon."
    assert "get_my_day_plan" not in "".join(published(events))


def test_only_registered_tool_names_with_call_brackets_are_removed():
    names = ["get_my_day_plan", "get_upcoming", "web_search"]
    assert mo.strip_call_text("`get_my_day_plan(day='tomorrow')`", names) == ""
    assert mo.strip_call_text("get_my_day_plan(day='tomorrow')", names) == ""
    assert mo.strip_call_text("You have 2 events. `get_upcoming()`", names) == "You have 2 events."
    assert mo.strip_call_text("```tool_code\nprint(get_my_day_plan(day='tomorrow'))\n```\nLunch at noon.",
                              names) == "Lunch at noon."
    for kept in ("Use f(x) = max_value(3) in math.", "Call web_search to look (it finds pages).",
                 "Lunch (with Dana) at noon."):
        assert mo.strip_call_text(kept, names) == kept
