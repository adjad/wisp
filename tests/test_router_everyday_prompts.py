"""Everyday prompts that used to route to the wrong (or no) tools.

Live-found on the 1.2.0 audit:
* "save a note: buy oat milk" was scoped to search_notes alone.
* "text +1 650 555 0134 that I'm outside" was routed as a messages LOOKUP.
* "move the first one to 4pm" right after a calendar read had no rule, so
  retrieval chose update_reminder for a calendar event and looped on it.
"""
import pytest

import service.tools  # noqa: F401  register the roster
import service.router.router as R
from service.tools.registry import REGISTRY

NOTE_WRITES = [
    "save a note: buy oat milk", "create a note called Trip", "write a note about the trip",
    "note: pick up dry cleaning", "save this to my notes: flight at 6", "add a note to call the dentist",
    "please save a note that the gate code changed", "put milk on my notes list",
    "take a note: meeting moved", "jot down that the wifi password changed",
]
NOTE_READS = [
    "what did I save in my notes", "search my notes for groceries", "show me my notes",
    "what's in my notes", "find the note about the trip", "read my note called Trip",
    "did I write a note about the wifi", "how many notes do I have",
]
PHONE_TEXTS = [
    "text +1 650 555 0134 that I'm outside", "text (650) 555-0134 I'm here",
    "text 650-555-0134 running late", "message +16505550134 hello", "text 650.555.0134 thanks",
]


@pytest.mark.parametrize("prompt", NOTE_WRITES)
def test_note_writes_are_recognised(prompt):
    assert R._NOTE_WRITE_RE.search(prompt)


@pytest.mark.parametrize("prompt", NOTE_READS)
def test_note_reads_never_arm_the_write_tools(prompt):
    assert not R._NOTE_WRITE_RE.search(prompt)


@pytest.mark.parametrize("prompt", ["save a note: buy oat milk", "write a note about the trip",
                                    "note: pick up dry cleaning", "save this to my notes: flight at 6"])
def test_note_write_routes_offer_create_note(prompt):
    decision = R.rule_route(prompt)
    assert decision is not None and "create_note" in (decision.tool_subset or ()), decision


@pytest.mark.parametrize("prompt", PHONE_TEXTS)
def test_texting_a_formatted_phone_number_is_a_send(prompt):
    assert R.SEND_MESSAGE_RE.search(prompt)
    decision = R.rule_route(prompt)
    assert decision is not None and "send_message" in (decision.tool_subset or ()), decision


@pytest.mark.parametrize("prompt", ["what's the phone number for Mom", "call 650-555-0134",
                                    "is 650-555-0134 a spam number"])
def test_a_phone_number_alone_is_not_a_text(prompt):
    assert not R.SEND_MESSAGE_RE.search(prompt)


EVENTS = "Upcoming — tomorrow (4 item(s))\nCalendar events: 4; Wisp/Apple reminders: 0. [Calendar event] and [Reminder] are shown in separate sections."
REMINDERS = "Upcoming — today\nCalendar events: 0; Wisp/Apple reminders: 3."
MIXED = "Calendar events: 2; Wisp/Apple reminders: 1."


def _edit(text, last_assistant, last_tools):
    decision = R._edit_reference_subset(text, last_assistant, last_tools)
    return None if decision is None else (decision, R._finalize(decision, text))


@pytest.mark.parametrize("text", ["move the first one to 4pm", "push that back an hour", "reschedule the second one to Friday",
                                  "change it to 5pm", "make it 5pm", "actually 5pm", "shift the last one to tomorrow"])
def test_editing_a_listed_calendar_event_requires_update_event_not_a_reminder_tool(text):
    decision, final = _edit(text, EVENTS, "get_upcoming")
    # Wisp deliberately cannot edit events; requiring update_event makes the loop
    # say so honestly instead of retrieval picking update_reminder and looping.
    assert ("update_event" in {t for g in decision.required_tool_groups for t in g})
    assert "update_reminder" in decision.forbidden_tools
    assert {"add_calendar_event", "cancel_event"} <= set(decision.forbidden_tools)
    assert REGISTRY["update_event"].unavailable_reason


@pytest.mark.parametrize("text", ["move the first one to 6pm", "push that to tomorrow", "change the second one to 7pm"])
def test_editing_a_listed_reminder_uses_update_reminder(text):
    decision, final = _edit(text, REMINDERS, "get_upcoming")
    assert "update_reminder" in final.tool_subset and "update_event" not in final.tool_subset
    assert not decision.required_tool_groups


def test_mixed_list_offers_the_reminder_edit_and_the_honest_event_path():
    decision, final = _edit("change that to 5pm", MIXED, "get_upcoming")
    assert "update_reminder" in final.tool_subset
    assert "add_calendar_event" in decision.forbidden_tools


@pytest.mark.parametrize("text", ["cancel the second one", "delete the first one", "remove that"])
def test_cancelling_a_listed_event_offers_cancel_not_an_edit(text):
    decision, final = _edit(text, EVENTS, "get_upcoming")
    assert "cancel_event" in final.tool_subset and "update_reminder" not in final.tool_subset
    assert not decision.required_tool_groups


def test_a_correction_right_after_adding_an_event_is_an_event_edit():
    decision, _ = _edit("actually make it 5pm", "Added Dentist Tue 3pm.", "add_calendar_event")
    assert "update_reminder" in decision.forbidden_tools


@pytest.mark.parametrize("text,last_assistant,last_tools", [
    ("move the first one to 4pm", "inbox digest", "summarize_emails"),   # not calendar items
    ("move the first one to 4pm", None, None),                            # no context at all
    ("what's on my calendar tomorrow", EVENTS, "get_upcoming"),           # a read, not an edit
    ("move the file to the desktop", EVENTS, "get_upcoming"),             # no list referent
])
def test_edit_rule_does_not_fire_outside_its_context(text, last_assistant, last_tools):
    assert R._edit_reference_subset(text, last_assistant, last_tools) is None


# The body of a message is content, not a command.
SEND_BODIES = [
    "text Mom running late", "message Sam running 5 minutes behind", "text Dad open the door for me",
    "text Sam call me back", "text Mom I'm running to the store", "text 650-555-0134 running late",
    "please text Alex run the report for me", "dm Sam quit stalling",
    "email bob@example.com the server is running slow",
]
MACHINE_OPS = ["run the tests", "open Safari", "quit Slack", "kill the process on port 3000",
               "what's running on my mac", "restart my computer", "launch Spotify"]


@pytest.mark.parametrize("prompt", SEND_BODIES)
def test_action_verbs_inside_a_message_body_do_not_hijack_the_send(prompt):
    decision = R.rule_route(prompt)
    assert decision is not None and decision.tool_subset, prompt
    assert {"send_message", "send_email"} & set(decision.tool_subset), decision.reason


@pytest.mark.parametrize("prompt", MACHINE_OPS)
def test_real_machine_requests_are_still_machine_requests(prompt):
    decision = R.rule_route(prompt)
    assert decision is not None
    assert not ({"send_message", "send_email"} & set(decision.tool_subset or ())), prompt


def test_message_body_is_stripped_only_after_the_recipient():
    assert R._without_message_body("text Mom running late") == "text Mom"
    assert R._without_message_body("please text 650-555-0134 open the door") == "please text 650-555-0134"
    # Not a message request: untouched.
    assert R._without_message_body("run the tests and text Mom") == "run the tests and text Mom"


# An authored message to a literal address or number binds the recipient in code.
ADDRESSED = [
    ("send jane.doe@example.com an email asking to move our meeting to Friday", "send_email", "jane.doe@example.com"),
    ("email bob@example.com saying I'll be late", "send_email", "bob@example.com"),
    ("email bob@example.com about Friday's plan", "send_email", "bob@example.com"),
    ("Email Sam@Example.com asking if he can reschedule", "send_email", "sam@example.com"),
    ("text +1 650 555 0134 that I'm outside", "send_message", "+1 650 555 0134"),
    ("draft an email to bob@example.com saying thanks for the help", "draft_email", "bob@example.com"),
]
NOT_ADDRESSED = [
    "email bob@example.com my calendar for tomorrow",                 # data delivery
    "email bob@example.com a summary of my unread emails",
    "what's the email bob@example.com sent me about Friday",          # a read
    "forward this to bob@example.com saying FYI please review",       # forwarding
    "email bob@example.com and alice@example.com saying hi there folks",   # two recipients
    "remind me to email bob@example.com about the invoice",
    "email bob@example.com saying hello tomorrow at 9am",             # send later
    "email bob@example.com saying hello later",
    "email bob@example.com in 10 minutes saying the build is done",
    "schedule an email to bob@example.com saying happy birthday",
    "text (650) 555-0134 I'm here",                                   # no authored-content cue
]


@pytest.mark.parametrize("prompt,first_tool,recipient", ADDRESSED)
def test_a_literal_recipient_is_bound_and_never_looked_up(prompt, first_tool, recipient):
    decision = R._explicit_address_compose(R._normalize_typos(prompt))
    assert decision is not None, prompt
    assert decision.tool_subset[0] == first_tool
    assert all(args == {"to": recipient} for args in decision.tool_argument_bindings.values())
    assert "lookup_contact" in decision.forbidden_tools and "lookup_contact" not in decision.tool_subset
    assert not ({"get_upcoming", "search_notes", "view_emails"} & set(decision.tool_subset))


@pytest.mark.parametrize("prompt", NOT_ADDRESSED)
def test_the_binding_rule_leaves_other_requests_alone(prompt):
    assert R._explicit_address_compose(R._normalize_typos(prompt)) is None


def test_the_rule_is_reached_through_rule_route():
    decision = R.rule_route("send jane.doe@example.com an email asking to move our meeting to Friday")
    assert decision.tool_argument_bindings["send_email"] == {"to": "jane.doe@example.com"}
    assert "explicit address" in decision.reason


@pytest.mark.parametrize("prompt,recipient", [
    ("email bob@example.com saying I'll be late", "bob@example.com"),
    ("text +1 650 555 0134 that I'm outside", "+1 650 555 0134"),
])
def test_the_model_is_told_the_recipient_is_already_resolved(prompt, recipient):
    decision = R._explicit_address_compose(R._normalize_typos(prompt))
    assert recipient in decision.resolved_request
    assert "Do not look anyone up" in decision.resolved_request
    assert decision.resolved_request.startswith(prompt)


# Formatted phone numbers are ONE recipient in the typed task compiler.
@pytest.mark.parametrize("prompt,who,body", [
    ("text +1 650 555 0134 that I'm outside", "+1 650 555 0134", "I'm outside"),
    ("text (650) 555-0134 saying I'm here", "(650) 555-0134", "I'm here"),
    ("text (650) 555-0134 I'm here", "(650) 555-0134", "I'm here"),
    ("text 650-555-0134 running late", "650-555-0134", "running late"),
    ("message +16505550134 saying hello", "+16505550134", "hello"),
    ("send a text to +1 650 555 0134 saying hi", "+1 650 555 0134", "hi"),
    ("send +1 650 555 0134 a text saying hi", "+1 650 555 0134", "hi"),
    ("text Mom that I'm late", "Mom", "I'm late"),
    ("text Mom running late", "Mom", "running late"),
    ("text Sam 5 minutes late", "Sam", "5 minutes late"),
])
def test_task_compiler_treats_a_formatted_number_as_one_recipient(prompt, who, body):
    from service.tasks import compiler as C
    match = C._MESSAGE_SEND_INTRO.match(prompt) or C._MESSAGE_SEND_BARE.match(prompt)
    groups = {k: v for k, v in match.groupdict().items() if v and k.startswith("who")}
    assert list(groups.values()) == [who]
    assert match.group("body") == body
