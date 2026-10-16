"""Router faults found while replaying the Mellum R1 fine-tune dataset.

Every item of that dataset went through the real route() + apply_session_pin
(PR #234, ROUTER_FINDINGS.md). About 300 could not be answered by ANY model:
the router never offered the tool the request needed, or forced a tool on a
request that needed none. These tests pin the intent/domain fixes, using
paraphrases rather than the dataset's own templates so the rules stay general.

Router only: no model, no embedding server (the stub embedder stands in), no
network. Run with:

    .venv/bin/python -m pytest -q tests/test_router_dataset_findings.py
"""
import asyncio

import pytest

import service.tools  # noqa: F401  register the roster
import service.router.router as R
from service.router.pinning import apply_session_pin
from service.tasks.compiler import reminder_request_clauses
from tests.stub_embedder import install as install_stub_embedder

install_stub_embedder()

SENDS = {"send_email", "send_message", "reply_to_email", "forward_email", "schedule_send"}


def _route(text, **kw):
    return apply_session_pin(asyncio.run(R.route(text, **kw)), None, text)


def _offered(decision):
    """What the model can call this turn (None subset = the whole registry)."""
    from service.tools.registry import REGISTRY
    tools = set(decision.tool_subset if decision.tool_subset is not None else REGISTRY)
    tools |= {n for n, _ in decision.direct_calls}
    if decision.force_first_tool:
        tools.add(decision.force_first_tool)
    return tools - set(decision.forbidden_tools) if decision.needs_tools else set()


# --- 1. Weather without the word "weather" never reached get_weather ----------

@pytest.mark.parametrize("prompt", [
    "what's the forecast for Tucson tomorrow?",
    "how's the weather shaping up in Seattle this weekend?",
    "will it be cold in Boston tonight?",
    "should I bring an umbrella, is it going to rain later?",
    "do I need a coat today?",
])
def test_weather_questions_offer_get_weather(prompt):
    d = _route(prompt)
    assert "get_weather" in _offered(d), d.reason
    assert ("web_search", {"query": prompt}) not in d.direct_calls, d.reason


def test_weather_news_and_explicit_search_keep_the_web_contract():
    assert "web_search" in {n for n, _ in _route("any news about the hurricane in Florida today?").direct_calls}
    assert "get_weather" not in _offered(_route("search the web for the forecast in Tucson tomorrow"))


def test_live_quote_phrasing_offers_get_stock_price():
    assert "get_stock_price" in _offered(_route("where's Tesla trading right now?"))


# --- 2. A biller's statement is in the user's mail, not on the web ------------

@pytest.mark.parametrize("prompt", [
    "what's the Discover bill this month?",
    "how much do I owe Barclays?",
    "check my Bank of America balance",
    "how much is my Synchrony payment this month?",
    "what's my credit card statement say?",
])
def test_card_balance_questions_read_mail(prompt):
    d = _route(prompt)
    assert "view_emails" in _offered(d), d.reason
    assert not any(n == "web_search" for n, _ in d.direct_calls), d.reason
    assert not (_offered(d) & SENDS)


def test_public_bill_questions_stay_public():
    d = _route("what's the average electricity bill in Texas this year?")
    assert "view_emails" not in _offered(d)
    assert "web_search" in _offered(d)


# --- 3. Replies on an unnamed channel never offered reply_to_email ------------
# They were treated as channel-ambiguous, so every send tool was withheld.

@pytest.mark.parametrize("prompt", [
    "reply to the plumber and say tuesday is fine",
    "write back to my landlady saying I'll be home",
    "answer the HOA's email: we'll attend the meeting",
    "answer the dentist's email: does the appointment run long too?",
])
def test_replies_offer_reply_tools_without_channel_clarify(prompt):
    d = _route(prompt)
    offered = _offered(d)
    assert "reply_to_email" in offered, d.reason
    assert "view_emails" in offered, d.reason
    assert not d.clarify_channel and "clarify" not in d.reason


def test_unnamed_reply_answers_mail_and_a_texted_reply_says_so():
    """Same convention as "reply to that" -> reply_to_email."""
    offered = _offered(_route("reply to the plumber and say tuesday is fine"))
    assert "send_message" not in offered
    texted = _offered(_route("text the plumber back and say tuesday is fine"))
    assert "send_message" in texted and "reply_to_email" not in texted


def test_answer_this_question_is_not_a_reply():
    assert not R._REPLY_INTENT_RE.search("answer this question: what is 2+2")


# --- 4. A fronted time adverbial split a reminder into two "tasks" ------------

@pytest.mark.parametrize("prompt", [
    "on thursday, remind me to call the vet",
    "next monday at 8am, remind me to submit the form",
    "tonight at 9, remind me to lock the back door",
])
def test_fronted_time_reminders_offer_add_reminder(prompt):
    assert reminder_request_clauses(prompt) == (prompt,)
    d = _route(prompt)
    assert "add_reminder" in _offered(d), d.reason
    assert "clarify" not in d.reason


def test_a_real_second_clause_is_still_split():
    assert len(reminder_request_clauses("tomorrow, check my email and remind me to call mom")) == 2


# --- 5. A named message is not necessarily a text; "no reply" is a read -------

@pytest.mark.parametrize("prompt", [
    "show me the Verizon message — no reply needed",
    "pull up that PG&E message, no need to respond",
    "open the Venmo email, don't reply to it",
    "read me the Comcast email without replying",
])
def test_named_message_reads_offer_email_and_no_sends(prompt):
    d = _route(prompt)
    assert "view_emails" in _offered(d), d.reason
    assert not (_offered(d) & SENDS), d.reason


def test_texts_from_a_person_stay_messages_only():
    offered = _offered(_route("any messages from Jordan?"))
    assert "view_messages" in offered and "view_emails" not in offered


# --- 6. Pure writing prompts were forced through a lookup ----------------------

@pytest.mark.parametrize("prompt", [
    "compose a sonnet about autumn leaves",
    "write a short graduation message for my niece",
    "can you write me a funny limerick about my cat",
    "give me a quick toast for my best friend's wedding",
    "write a thank-you note for my neighbor",
])
def test_creative_writing_needs_no_tool(prompt):
    d = _route(prompt)
    assert not d.needs_tools and not d.force_first_tool and not d.direct_calls, d.reason


@pytest.mark.parametrize("prompt", [
    "write a birthday message to Jordan and text it to her",
    "compose a poem and email it to my mom",
    "write a poem about my notes",
])
def test_writing_that_delivers_or_reads_keeps_tools(prompt):
    assert _route(prompt).needs_tools


# --- 7. Smaller families ---------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    "what's monday looking like?", "is friday busy for me?", "anything planned for saturday?",
    "do I have anything on wednesday", "anything happening this weekend?",
])
def test_bare_day_agenda_questions_read_the_calendar(prompt):
    d = _route(prompt)
    assert "get_upcoming" in _offered(d), d.reason
    assert not any(n == "web_search" for n, _ in d.direct_calls)


@pytest.mark.parametrize("reply", ["say sounds good", "tell her yes", "ok reply that works"])
def test_short_reply_after_a_text_thread_offers_send_message(reply):
    d = _route(reply, last_user="any texts from Maya?",
               last_assistant='Maya asked: "Lunch on Friday?"', last_tools="view_messages")
    assert "send_message" in _offered(d), d.reason


def test_say_it_again_is_not_a_reply():
    d = _route("say that again", last_user="any texts from Maya?",
               last_assistant='Maya asked: "Lunch on Friday?"', last_tools="view_messages")
    assert "send_message" not in _offered(d)


@pytest.mark.parametrize("prompt", [
    "where's my cover letter draft?", "where did I save my lease?", "find my thesis draft",
])
def test_document_location_questions_offer_find_files(prompt):
    d = _route(prompt)
    assert "find_files" in _offered(d), d.reason
    assert not (_offered(d) & SENDS)


def test_draft_the_verb_still_composes():
    assert "draft_email" in _offered(_route("draft a reply to the landlord's email"))


@pytest.mark.parametrize("prompt,tool", [
    ("quit Mail", "quit_app"), ("close Reminders for me", "quit_app"), ("launch Notes", "open_app"),
])
def test_named_app_commands_offer_the_app_tool(prompt, tool):
    d = _route(prompt)
    assert tool in _offered(d), d.reason


def test_lowercase_open_my_calendar_keeps_its_ambiguity():
    assert _route("open my calendar").reason != "named app command -> open_app"


@pytest.mark.parametrize("prompt", [
    "new note — Packing: socks, charger", "new note: call the bank about the card",
])
def test_new_note_creates_a_note(prompt):
    d = _route(prompt)
    assert "create_note" in _offered(d), d.reason


def test_whose_email_address_is_a_contact_lookup():
    d = _route("what's dad's email?")
    assert "lookup_contact" in _offered(d) and "view_emails" not in _offered(d)
    assert "view_emails" in _offered(_route("read dad's email"))


def test_remind_me_what_is_a_question_not_a_reminder():
    d = _route("remind me what I had on last friday")
    assert "get_past_events" in _offered(d) and "add_reminder" not in _offered(d), d.reason
    assert "add_reminder" in _offered(_route("remind me to water the plants at 6pm"))


@pytest.mark.parametrize("prompt", [
    "block off thursday at 3pm for a dentist visit",
    "set up a call with the accountant tomorrow at 11am",
])
def test_calendar_writes_without_a_calendar_noun(prompt):
    assert "add_calendar_event" in _offered(_route(prompt))


def test_supposed_to_remember_reads_reminders():
    assert {"get_upcoming", "search_reminders"} <= _offered(_route("is there anything I'm supposed to remember?"))


@pytest.mark.parametrize("prompt", ["ok send it", "go ahead and send that"])
def test_bare_send_with_no_conversation_offers_nothing_to_send(prompt):
    d = _route(prompt)
    assert not d.needs_tools and not (_offered(d) & SENDS)
