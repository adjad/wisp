"""Three Ask failures from a real-model run of a 0.5.0 preview (2026-11-13),
described in synthetic words:
- "Delete the <name> note": delete_note was called, policy said confirm, and
  the turn crashed before its card (the loop used asyncio without importing it).
- "Delete that reminder" right after one was made went to a bulk clearer, then
  to cancel_event with no usable title. Nothing was removed.
- "Reply to Dana's last email and say yes" searched mail for the instruction's
  own words and said nothing was found.

Scripted model clients and synthetic tools through the real loop. No model,
server lifespan, native tool, network or user data.
"""
import pytest

from service import model_only as mo
from service.tools import notes_write
from tests.test_model_led_integration import Approval, call, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

OWNER = {"claim_effect": lambda tool, args: {"admitted": True},
         "finish_effect": lambda claim, ok: True}
TITLE = {"type": "object", "properties": {"title": {"type": "string"}, "folder": {"type": "string"},
                                          "query": {"type": "string"}, "scope": {"type": "string"}},
         "additionalProperties": False}


# --- 1. delete_note reaches its card ---------------------------------------------

def delete_turn(register, approval):
    register("delete_note", "notes_write", "notes_write", parameters=TITLE)
    return run([[call("delete_note", title="Wisp test")], "Deleted the Wisp test note."],
               prompt="Delete the Wisp test note", ask_quality=True, approval=approval,
               model_led_preload=["delete_note"], **OWNER)


def test_delete_note_shows_its_card_and_runs_only_when_approved(synthetic, monkeypatch):
    effects, register = synthetic
    monkeypatch.setattr(notes_write, "_find_notes", lambda title, folder: [("n1", "f1", "Work", "Wisp test")])
    approval = Approval(True)
    delete_turn(register, approval)
    assert [a["tool"] for a in approval.calls] == ["delete_note"]
    card = approval.calls[0]
    assert card["args"] == {"title": "Wisp test", "folder": "Work"}        # bound to the one note's folder
    assert "“Wisp test”" in card["reason"] and "“Work”" in card["reason"] and "always confirmed" in card["reason"]
    assert effects == [("delete_note", {"title": "Wisp test", "folder": "Work"})]


def test_delete_note_declined_on_the_card_deletes_nothing(synthetic, monkeypatch):
    effects, register = synthetic
    monkeypatch.setattr(notes_write, "_find_notes", lambda title, folder: [("n1", "f1", "Work", "Wisp test")])
    approval = Approval(False)
    delete_turn(register, approval)
    assert [a["tool"] for a in approval.calls] == ["delete_note"] and effects == []


def test_a_failed_notes_lookup_still_shows_the_card(synthetic, monkeypatch):
    effects, register = synthetic
    def broken(title, folder):
        raise TimeoutError("Notes did not answer")
    monkeypatch.setattr(notes_write, "_find_notes", broken)
    approval = Approval(False)
    delete_turn(register, approval)
    assert [a["tool"] for a in approval.calls] == ["delete_note"]
    assert "“Wisp test”" in approval.calls[0]["reason"] and effects == []


# --- 2. "that reminder" is the one just made in this chat ------------------------

MADE = "Remind me to call the dentist tomorrow at 9am"
SAID = "I set a reminder to call the dentist for tomorrow at 9:00 AM."
ROWS = [{"kind": "reminder", "title": "Call the dentist"}, {"kind": "reminder", "title": "Pay rent"},
        {"kind": "event", "title": "Dentist"}]


@pytest.mark.parametrize("prompt", ["Delete that reminder", "delete that reminder please", "Cancel the reminder",
                                    "Actually, remove this reminder.", "delete the reminder you just set"])
def test_that_reminder_resolves_to_the_one_this_chat_made(monkeypatch, prompt):
    monkeypatch.setattr(mo, "_recent_reminders", lambda: [r for r in ROWS if r["kind"] == "reminder"])
    assert mo.chat_reminder(prompt, MADE, SAID) == "Call the dentist"
    assert "Call the dentist" in mo.chat_reminder_hint("Call the dentist")


@pytest.mark.parametrize("prompt,previous", [
    ("Delete the dentist reminder", MADE),         # names one itself: the title path
    ("Delete all my reminders", MADE),
    ("Delete that reminder", "what's the weather"),  # nothing this chat made
    ("What is that reminder for?", MADE),
])
def test_other_wordings_and_other_chats_resolve_nothing(monkeypatch, prompt, previous):
    monkeypatch.setattr(mo, "_recent_reminders", lambda: [r for r in ROWS if r["kind"] == "reminder"])
    assert mo.chat_reminder(prompt, previous, "Okay.") == ""


def test_delete_that_reminder_shows_the_card_for_that_one(synthetic, monkeypatch):
    effects, register = synthetic
    monkeypatch.setattr(mo, "_recent_reminders", lambda: [ROWS[0]])
    for name in ("cancel_event", "clear_reminders", "clear_past_reminders"):
        register(name, "assistant_tools", "calendar_write", parameters=TITLE)
    approval = Approval(True)
    client, _, _ = run(
        [[call("clear_reminders", query="that reminder")], [call("cancel_event", title="that reminder")],
         "Cancelled it."],
        prompt="Delete that reminder", ask_quality=True, approval=approval, model_led_preload=["cancel_event"],
        history=({"role": "user", "content": MADE}, {"role": "assistant", "content": SAID}), **OWNER)
    assert effects == [("cancel_event", {"title": "Call the dentist"})]
    assert [(a["tool"], a["args"]) for a in approval.calls] == [("cancel_event", {"title": "Call the dentist"})]
    offered = {s["function"]["name"] for s in client.requests[0].get("tools") or []}
    assert "cancel_event" in offered and not offered & mo.ALL_REMINDER_CLEARERS


def test_the_real_head_start_leads_with_the_single_delete():
    import service.tools  # noqa: F401  (registers the real tools)
    from service import main
    assert main._ask_tool_rank("Delete that reminder", MADE)[0][0] == "cancel_event"


# --- 3. "Reply to Dana's last email and say yes" ----------------------------------

@pytest.mark.parametrize("prompt,who", [
    ("Reply to Dana's last email and say yes", "Dana"),
    ("reply to Dana Lee's email saying I'll be there", "Dana Lee"),
    ("Respond to the latest email from Dana and say yes", "Dana"),
    ("Answer my professor's email and say thanks", "professor"),
    ("Reply to Dana and say yes", ""),               # no email named: mail or a text
    ("Draft a reply to Dana's last email", ""),      # writing: reply_reads covers it
    ("Did Dana reply to my email?", ""),
    ("Reply to the last email", ""),
])
def test_the_sender_of_the_email_to_answer(prompt, who):
    assert mo.email_reply_sender(prompt) == who


def test_the_search_is_the_senders_name_not_the_instruction():
    assert mo.reply_search_args({"query": "Reply to Dana"}, "Dana") == {"query": "Dana"}
    assert mo.reply_search_args({"query": "Dana's last email", "count": 3}, "Dana") == {"query": "Dana", "count": 3}
    assert mo.reply_search_args({}, "Dana") == {"query": "Dana"}
    assert mo.reply_search_args({"query": "Dana"}, "Dana") == {"query": "Dana"}
    assert mo.reply_search_args({"query": "Dana invoice"}, "Dana") == {"query": "Dana invoice"}
    hint = mo.email_reply_hint("Dana")
    assert 'query="Dana"' in hint and "reply_to_email" in hint


def test_reply_turn_reads_danas_mail_then_shows_the_reply_card(synthetic, monkeypatch):
    effects, register = synthetic
    register("view_emails", "email_tools", parameters={
        "type": "object", "properties": {"query": {"type": "string"}}, "additionalProperties": False})
    register("reply_to_email", "action_tools", "email_send", parameters={
        "type": "object", "properties": {"message_id": {"type": "string"}, "body": {"type": "string"}},
        "required": ["message_id", "body"], "additionalProperties": False})

    async def prepared(args):
        return {**args, "account": "Synthetic"}, ""
    monkeypatch.setattr("service.tools.action_tools.prepare_reply_args", prepared)
    approval = Approval(False)
    run([[call("view_emails", query="Reply to Dana")],
         [call("reply_to_email", message_id="<m1@example.test>", body="Yes, that works for me.")],
         "I didn't send it."],
        prompt="Reply to Dana's last email and say yes", ask_quality=True, approval=approval,
        model_led_preload=["view_emails", "reply_to_email"], **OWNER)
    assert effects == [("view_emails", {"query": "Dana"})]                # the name, and nothing sent
    assert [a["tool"] for a in approval.calls] == ["reply_to_email"]
    assert approval.calls[0]["args"]["message_id"] == "<m1@example.test>"
    assert approval.calls[0]["args"]["body"] == "Yes, that works for me."


def test_the_real_head_start_reads_mail_then_replies():
    import service.tools  # noqa: F401
    from service import main
    assert main._ask_tool_rank("Reply to Dana's last email and say yes", "")[0][:2] == ["view_emails", "reply_to_email"]


# --- 4. a change said to be under way, with nothing behind it ---------------------

MOVING = ("I'm moving your Coffee with Alex Chen from 3:00 PM to 4:00 PM today. "
          "I'll let you know as soon as that change is confirmed.")


@pytest.mark.parametrize("text", [
    MOVING, "I'm sending that email to Priya now.", "I am now adding Demo point 4 to the note.",
    "I'll let you know as soon as that change is confirmed.", "I will confirm once the reminder is set.",
    "Okay, I'm cancelling the dentist reminder.",
])
def test_in_progress_and_promised_changes_are_claims(text):
    assert mo.claimed_write(text)


@pytest.mark.parametrize("text", [
    "I'm not moving anything until you confirm.", "I'll move it once you tell me the time.",
    "Should I move it to 4?", "I'm looking at your calendar for today.", "I'll let you know what I find.",
    "You have Coffee with Alex Chen at 3:00 PM.",
])
def test_offers_refusals_and_reads_are_not(text):
    assert not mo.claimed_write(text)


def test_a_move_that_only_read_the_calendar_cannot_end_on_im_moving(synthetic):
    effects, register = synthetic
    register("update_event", "assistant_tools", "calendar_write", parameters=TITLE)
    client, events, answer = run(
        [[call("get_upcoming", days=1)], MOVING, MOVING, "never reached"],
        prompt="Move my 3pm with Alex Chen today to 4", ask_quality=True,
        model_led_preload=["update_event", "get_upcoming"])
    assert [name for name, _ in effects] == ["get_upcoming"]
    assert "Nothing has run yet, so that was not done." in client.requests[2]["messages"][-1]["content"]
    assert answer == mo.ASK_CLAIM_NOT_DONE


# --- 5. the channel is the person's word ------------------------------------------

@pytest.mark.parametrize("prompt,barred", [
    ("Email Priya Shah that I'll be ten minutes late", {"send_message"}),
    ("please send an email to Priya saying hi", {"send_message"}),
    ("Text Priya that I'll be ten minutes late", {"send_email"}),
    ("Message Dana I'm on my way", {"send_email"}),
    ("Email Priya the text of my note", set()),          # both words: left to the model
    ("Text Dana my email address", set()),
    ("Tell Priya I'll be late", set()),
    ("What did Priya email me?", set()),
])
def test_only_the_named_channels_send_is_offered(prompt, barred):
    registry = {"send_message": None, "send_email": None}
    assert mo.other_channel_sends(prompt) == barred
    assert mo.ask_forbidden_tools(prompt, registry) == barred
