"""Everyday prompts that used to route to the wrong (or no) tools.

Live-found on the 1.2.0 audit:
* "save a note: buy oat milk" was scoped to search_notes alone.
* "text +1 650 555 0134 that I'm outside" was routed as a messages LOOKUP.
* "move the first one to 4pm" right after a calendar read had no rule, so
  retrieval chose update_reminder for a calendar event and looped on it.
"""
import pytest
import asyncio
import json
from dataclasses import replace

from tests.test_fast_path_intent import inert_endpoint, successful_reminder_endpoint

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


_ADDRESS_BODIES = [
    "please email jane@example.com", "please call +16505550999",
    "first line\nplease email jane@example.com\nplease call +16505550999",
]


@pytest.mark.parametrize("body", _ADDRESS_BODIES)
@pytest.mark.parametrize("quotes", [("", ""), ('"', '"'), ('“', '”'), ('`', '`')])
@pytest.mark.parametrize("header", ["text Mom", "email Mom"])
def test_body_only_literals_never_supply_compose_recipient(header, body, quotes):
    left, right = quotes
    assert R._explicit_address_compose(f"{header} saying {left}{body}{right}") is None


@pytest.mark.parametrize("header", [
    "text jane@example.com", "email +16505550999",
    "text Mom at +16505550999", "email Mom at jane@example.com",
    "email jane@example.com and Mom", "text Mom or +16505550999",
])
def test_compose_header_must_be_one_literal_recipient_in_the_requested_channel(header):
    assert R._explicit_address_compose(header + " saying please bring milk") is None


@pytest.mark.parametrize("body", _ADDRESS_BODIES)
@pytest.mark.parametrize("quotes", [("", ""), ('"', '"'), ('“', '”')])
@pytest.mark.parametrize("header,tool,target", [
    ("email bob@example.com", "send_email", "bob@example.com"),
    ("text +1 650 555 0134", "send_message", "+1 650 555 0134"),
    ("send bob@example.com an email", "send_email", "bob@example.com"),
    ("draft an email to bob@example.com", "draft_email", "bob@example.com"),
])
def test_only_header_literal_binds_even_when_body_mentions_other_addresses(
        header, tool, target, body, quotes):
    left, right = quotes
    decision = R._explicit_address_compose(f"{header} saying {left}{body}{right}")
    assert decision is not None and decision.tool_subset[0] == tool
    assert all(args == {"to": target} for args in decision.tool_argument_bindings.values())
    assert "lookup_contact" in decision.forbidden_tools


@pytest.mark.parametrize("body", _ADDRESS_BODIES)
@pytest.mark.parametrize("quotes", [("", ""), ('"', '"'), ('“', '”')])
@pytest.mark.parametrize("recipient", ["Mom", "+16505550134"])
@pytest.mark.parametrize("contact_found,approved", [(True, True), (True, False), (False, True)])
def test_body_address_actual_endpoint_keeps_named_recipient(
        inert_endpoint, monkeypatch, contact_found, approved, recipient, body, quotes):
    from service import main
    from service.tools import imessage_tools
    request, streams, calls = inert_endpoint
    lookups, approvals, routed = [], [], []
    left, right = quotes
    prompt = f"text {recipient} saying {left}{body}{right}"

    def contacts(name):
        lookups.append(name)
        return ([{"name": "Mom", "handles": ["+16505550134"], "preferred": "+16505550134"}]
                if contact_found else [])

    async def confirm(self, item):
        approvals.append(item)
        return approved

    async def send(**kwargs):
        calls.append(("send_message", kwargs))
        return "Message sent to " + kwargs["to"] + ": " + kwargs["text"]

    original_route = main.route

    async def record_route(*args, **kwargs):
        decision = await original_route(*args, **kwargs)
        routed.append(decision.as_dict())
        return decision

    monkeypatch.setattr(imessage_tools, "find_contacts", contacts)
    monkeypatch.setattr(main.InteractiveApprover, "confirm", confirm)
    monkeypatch.setattr(main, "route", record_route)
    monkeypatch.setitem(REGISTRY, "send_message", replace(REGISTRY["send_message"], func=send))
    direct_route = asyncio.run(R.route(prompt))
    events = asyncio.run(request(prompt))
    print(json.dumps({"prompt": prompt, "contact_found": contact_found,
                      "direct_route_bindings": direct_route.tool_argument_bindings,
                      "direct_route_tools": direct_route.tool_subset,
                      "endpoint_routes": routed, "lookups": lookups,
                      "approvals": approvals, "effects": calls}, default=str))
    assert lookups == (["Mom"] if recipient == "Mom" else [])
    resolved = contact_found or recipient != "Mom"
    expected = {"to": "+16505550134", "text": body}
    assert calls == ([("send_message", expected)] if resolved and approved else [])
    assert [item["args"] for item in approvals] == ([expected] if resolved else [])
    assert not any(args.get("to") in {"jane@example.com", "+16505550999"}
                   for args in direct_route.tool_argument_bindings.values())
    assert streams == [] and events[-1]["type"] == "done"


@pytest.mark.parametrize("prompt", [
    'Do not text Mom saying "please email jane@example.com"',
    'Explain the phrase "text Mom saying please email jane@example.com"',
    'What does "email bob@example.com saying hello there" mean?',
])
def test_literal_body_discussion_and_negation_have_no_effects(inert_endpoint, monkeypatch, prompt):
    from service import main
    from service.tools import imessage_tools
    request, streams, calls = inert_endpoint
    approvals = []

    async def confirm(self, item):
        approvals.append(item)
        return False

    monkeypatch.setattr(main.InteractiveApprover, "confirm", confirm)
    monkeypatch.setattr(imessage_tools, "find_contacts", lambda _name: [])
    events = asyncio.run(request(prompt))
    assert calls == [] and approvals == [] and events[-1]["type"] == "done"


@pytest.mark.parametrize("body", _ADDRESS_BODIES)
@pytest.mark.parametrize("receipt,valid", [
    ("Mom: +1 (650) 555-0134", True), ("Dad: +16505550135", False), ("No matching contacts", False),
])
@pytest.mark.parametrize("send_first", [True, False])
def test_body_literals_in_named_companions_do_not_bypass_contact_proof(
        successful_reminder_endpoint, monkeypatch, body, receipt, valid, send_first):
    from service import main
    from service.agent import loop
    from service.safety.policy import Tier, Decision
    request, streams, calls, assistant = successful_reminder_endpoint
    approvals, attempted = [], set()

    async def confirm(self, item):
        approvals.append(item)
        return True

    async def lookup(**args):
        calls.append(("lookup_contact", args))
        return receipt

    async def send(**args):
        calls.append(("send_message", args))
        return "Message sent to " + args["to"] + ": " + args["text"]

    async def scripted(_model, messages, **kwargs):
        streams.append(kwargs)
        offered = {tool["function"]["name"] for tool in kwargs.get("tools", [])}
        proposed = [
            ("lookup_contact", {"name": "Dad"}), ("set_volume", {"level": 0}),
            ("send_message", {"to": "+16505550999", "text": "wrong model body"}),
            ("add_reminder", {"title": "wrong model title", "when_iso": "2099-01-01T09:00"}),
        ]
        actions = [(name, args) for name, args in proposed
                   if name not in attempted and name in offered]
        attempted.update(name for name, _ in actions)
        yield {"kind": "final", "message": ({"role": "assistant", "content": "", "tool_calls": [
            {"id": "fixture-" + name, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}} for name, args in actions
        ]} if actions else {"role": "assistant", "content": "Synthetic completion."})}

    monkeypatch.setattr(main.InteractiveApprover, "confirm", confirm)
    monkeypatch.setattr(main.client, "stream_events", scripted)
    monkeypatch.setitem(REGISTRY, "lookup_contact", replace(REGISTRY["lookup_contact"], func=lookup))
    monkeypatch.setitem(REGISTRY, "send_message", replace(REGISTRY["send_message"], func=send))
    monkeypatch.setattr(loop, "decide", lambda _category, _args, *, tool=None: Decision(
        Tier.CONFIRM if tool == "send_message" else Tier.ALLOW, "synthetic only"))
    clauses = [f'Text Mom saying "{body}"', "Remind me to buy milk tomorrow"]
    if not send_first:
        clauses.reverse()
    events = asyncio.run(request("Mute my volume; " + "; ".join(clauses)))
    assert [args for name, args in calls if name == "lookup_contact"] == [{"name": "Mom"}]
    if valid:
        expected = {"to": "+1 (650) 555-0134", "text": body}
        assert [args for name, args in calls if name == "send_message"] == [expected]
        assert [item["args"] for item in approvals] == [expected]
        assert len(calls) == 4 and ("set_volume", {"level": 0}) in calls
        assert [row[0] for row in assistant._db.execute("SELECT title FROM commitments")] == ["buy milk"]
    else:
        assert calls == [("lookup_contact", {"name": "Mom"})] and approvals == []
        assert assistant._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0
    assert events[-1]["type"] == "done"
