"""Structural routing fixes measured on the routing-quality corpus.

See docs/ROUTING_DIAGNOSIS.md. Router only: no model, embedding server, tool
body or network. Retrieval uses the packaged lexical provider (and the stub
embedder, should a configuration select the embedding provider).
"""
import asyncio
import json

import pytest

import service.tools  # noqa: F401  register the roster
import service.router.router as R
from service.router import reranker, semantic
from tests.stub_embedder import install as install_stub_embedder

install_stub_embedder()

SENDS = {"send_email", "send_message", "reply_to_email", "forward_email", "schedule_send", "place_call"}
BULK_DELETES = {"clear_reminders", "clear_past_reminders", "clear_memory", "delete_path"}


def _route(text, **kw):
    return asyncio.run(R.route(text, **kw))


def _reachable(decision):
    tools = set(decision.tool_subset or ()) if decision.needs_tools else set()
    tools |= {n for n, _ in decision.direct_calls}
    tools |= {n for g in decision.required_tool_groups for n in g}
    return tools - set(decision.forbidden_tools)


# --- Fix 1: retrieval offers sends / bulk deletes only on that intent ---------

NO_OUTBOUND = [
    "tell me a joke", "write a haiku about autumn", "explain recursion like i'm five",
    "what's the capital of france", "do i have anything on sunday", "find me a free hour tomorrow",
    "what's my bike lock code", "ping me at 6 to start dinner", "write a short bio for my linkedin",
    "what does 'send it' mean in climbing slang",
]


@pytest.mark.parametrize("prompt", NO_OUTBOUND)
def test_retrieved_menu_has_no_committing_outbound_tool_without_outbound_intent(prompt):
    decision = _route(prompt)
    assert not (_reachable(decision) & SENDS), decision.reason


@pytest.mark.parametrize("prompt", ["what's on my calendar this weekend", "is there anything on my cal",
                                    "what time is sunset", "what's the difference between a list and a tuple"])
def test_retrieved_menu_has_no_bulk_delete_without_delete_intent(prompt):
    assert not (_reachable(_route(prompt)) & BULK_DELETES)


@pytest.mark.parametrize("prompt,tool", [
    ("tell Sam I'm running late", "send_message"), ("let dad know i landed", "send_message"),
    ("text alex ok sounds good", "send_message"), ("shoot bob an email saying hi", "send_email"),
    ("reply to that", "reply_to_email"), ("forward it to Dana", "forward_email"),
    ("call the dentist", "place_call"), ("drop Priya a line about friday", "send_email"),
])
def test_outbound_intent_keeps_the_send_tool_reachable(prompt, tool):
    assert semantic.gated_tool_allowed(tool, prompt)
    # Whatever outbound tool lexical retrieval ranks, the gate keeps it.
    ranked = {t for t in reranker.lexical_shortlist(prompt, limit=20) if t in SENDS}
    assert ranked <= set(reranker.lexical_candidates(prompt, writing=True))


@pytest.mark.parametrize("prompt", ["delete everything", "clear my reminders", "forget what I told you",
                                    "wipe the old logs", "get rid of past reminders"])
def test_delete_intent_keeps_bulk_delete_tools_eligible(prompt):
    assert any(semantic.gated_tool_allowed(t, prompt) for t in BULK_DELETES)


@pytest.mark.parametrize("prompt", ["tell me a joke", "ping me at 6", "let me know if it rains",
                                    "what's my schedule"])
def test_gate_does_not_open_on_lookalike_words(prompt):
    assert not any(semantic.gated_tool_allowed(t, prompt) for t in SENDS)


def test_gate_leaves_every_other_tool_alone():
    assert semantic.gated_tool_allowed("get_upcoming", "tell me a joke")
    assert semantic.gated_tool_allowed("draft_email", "write a haiku")


# --- Fix 2: a bare assent to a pending LOCAL offer is not a zero-tool turn ---

RELATIVE_OFFER = "You have a dentist appointment tomorrow at 3pm. Want me to set a reminder an hour before?"


@pytest.mark.parametrize("ack", ["sure", "ok", "yes please", "go ahead", "yeah do it"])
@pytest.mark.parametrize("history", [{}, {"last_user": "what's on tomorrow"},
                                     {"last_user": "what's on tomorrow", "recent_users": ["what's on tomorrow"]}])
def test_assent_to_relative_reminder_offer_looks_up_but_never_writes(ack, history):
    decision = _route(ack, last_assistant=RELATIVE_OFFER, last_tools="get_upcoming", **history)
    reach = _reachable(decision)
    assert decision.needs_tools and "get_upcoming" in reach, decision.reason
    assert decision.force_first_tool == "get_upcoming"
    # No resolvable date/clock in the offer: never a reminder write from "sure".
    assert "add_reminder" not in reach and "add_reminder" in decision.forbidden_tools
    assert not any("add_reminder" in g for g in decision.required_tool_groups)
    assert not (reach & SENDS)


def test_assent_to_a_dated_reminder_offer_with_history_still_never_writes():
    decision = _route("sure", last_user="when is my dentist appointment",
                      last_assistant="It's Thursday at 3pm. Want me to set a reminder for 2pm tomorrow?",
                      last_tools="get_upcoming")
    assert decision.needs_tools and decision.tool_subset == ["get_upcoming"]
    assert decision.reminder_action == "clarify_time"
    assert "add_reminder" in decision.forbidden_tools


def test_typed_time_answer_after_the_clarification_can_write():
    decision = _route("2pm", last_user="sure",
                      last_assistant="What time should I remind you before your dentist appointment?",
                      last_tools="get_upcoming")
    assert "add_reminder" in _reachable(decision)


def test_assent_to_an_open_offer_with_history_offers_the_open_tools():
    decision = _route("go ahead", last_user="find my resume",
                      last_assistant="I found resume_2026.pdf in Documents. Want me to open it?",
                      last_tools="find_files")
    assert decision.needs_tools and "open_app" in _reachable(decision)
    assert not (_reachable(decision) & SENDS)


@pytest.mark.parametrize("prior", ["search the web for Zorvia news; email it to Mom",
                                   "search the web for Zorvia news"])
def test_public_web_history_still_blocks_replay_through_an_unrelated_offer(prior):
    decision = _route("yes please", last_user=prior, last_assistant="Shall I run it?\n```bash\nls\n```",
                      last_tools="send_email, web_search")
    assert not decision.needs_tools and not decision.tool_subset


@pytest.mark.parametrize("prior", ["Don't open anything, just find my resume", "how do I open a pdf"])
def test_declined_or_meta_history_keeps_an_assent_tool_free(prior):
    decision = _route("go ahead", last_user=prior,
                      last_assistant="I found resume_2026.pdf in Documents. Want me to open it?",
                      last_tools="find_files")
    assert not decision.needs_tools


# --- Fix 3: "any new emails?" is the user's inbox, not current public info ----

@pytest.mark.parametrize("prompt,family", [
    ("any new emails?", {"view_emails", "summarize_emails", "triage_inbox"}),
    ("any important mail this morning", {"view_emails", "summarize_emails", "triage_inbox"}),
    ("did I get any new emails today", {"view_emails", "summarize_emails", "triage_inbox"}),
    ("any new texts?", {"view_messages", "summarize_messages"}),
    ("got any messages yet", {"view_messages", "summarize_messages"}),
])
def test_inbox_questions_read_the_inbox_not_the_web(prompt, family):
    decision = _route(prompt)
    reach = _reachable(decision)
    assert "web_search" not in reach and not decision.direct_calls, decision.reason
    assert reach & family, decision.reason


@pytest.mark.parametrize("prompt", ["any new iphone news", "any new covid guidance today",
                                    "latest news about the fed"])
def test_public_current_questions_still_search_the_web(prompt):
    assert "web_search" in _reachable(_route(prompt))


# --- Fix 4: a draft request keeps its draft tool; sends stay forbidden -------

@pytest.mark.parametrize("prompt,draft", [
    ("draft a reply to Priya but don't send it", "draft_email"),
    ("compose an email to my professor asking for an extension but don't send", "draft_email"),
    ("draft a text to mom about thanksgiving but don't send", "draft_message"),
    ("help me word a text to my landlord about the rent, don't send it", "draft_message"),
    ("prepare an email to HR asking about PTO, I'll send it myself", "draft_email"),
])
def test_draft_request_with_a_send_prohibition_offers_the_draft_not_the_send(prompt, draft):
    decision = _route(prompt)
    reach = _reachable(decision)
    assert draft in reach, decision.reason
    assert not (reach & SENDS), decision.reason


@pytest.mark.parametrize("prompt", ["summarize my emails, do not reply to anyone",
                                    "read my texts from mom, don't text her back",
                                    "find the lease email but don't send anything"])
def test_send_prohibition_without_a_draft_request_offers_no_send(prompt):
    assert not (_reachable(_route(prompt)) & SENDS)


def test_draft_and_then_send_is_still_a_send():
    assert not R._positive_draft_request("don't draft anything, just send it to Sam")
    assert "send_email" in _reachable(_route("draft an email to dana@example.com and send it"))


# --- Fix 5: typo/shorthand fall-through re-route ---------------------------

from service.router.normalize import normalize_typos  # noqa: E402


@pytest.mark.parametrize("prompt,tool", [
    ("txt alex ok sounds good", "send_message"),
    ("remeber that sam is vegetarian", "remember"),
    ("whats on my calender tmrw", "get_upcoming"),
])
def test_typo_requests_reach_the_rule_the_spelled_out_request_reaches(prompt, tool):
    decision = _route(prompt)
    assert decision.source != "default", decision.reason
    assert tool in _reachable(decision), decision.reason


def test_typo_fragment_continues_the_previous_calendar_read():
    decision = _route("and tmrw?", last_user="what's on my calendar today",
                      last_assistant="Today you have a 2pm call with HR.", last_tools="get_upcoming")
    assert "get_upcoming" in _reachable(decision), decision.reason


@pytest.mark.parametrize("text", ["text Cal that I'm late", "email Emil the notes",
                                  "whether or not it rains", "u up?", "tell me a joke"])
def test_normalization_leaves_names_and_real_words_alone(text):
    assert normalize_typos(text) == text


def test_normalization_is_a_fixed_point():
    once = normalize_typos("pls chek my calender tmrw and txt mom")
    assert once == "please check my calendar tomorrow and text mom"
    assert normalize_typos(once) == once


# --- P1 repair (review of PR #159): normalization is a route hint only ------

import itertools  # noqa: E402

REVIEW_P1 = [
    "emial appt@clinic.org about my visit", "emial bob@txt.att.net", "emial bob@msg.com",
    "print report.txt", "what is in budget.txt", "trash ~/Desktop/old.txt", "zip up taxes.txt",
    "share lyrics.txt with Dan", 'emial bob@example.com saying "txt me tmrw"',
    "what does tmr stand for in medicine", "the word emial is misspelled", "what does appt mean",
    "read notes.txt on my desktop", "open https://example.com/txt/appt?msg=tmrw",
    "emial dana@example.com the file `notes.txt`", "rename msg_2024.txt to appt-2025.txt",
]


@pytest.mark.parametrize("text,kept", [
    ("emial appt@clinic.org about my visit", "appt@clinic.org"),
    ("emial bob@txt.att.net", "bob@txt.att.net"), ("emial bob@msg.com", "bob@msg.com"),
    ("print report.txt", "report.txt"), ("what is in budget.txt", "budget.txt"),
    ("trash ~/Desktop/old.txt", "~/Desktop/old.txt"), ("zip up taxes.txt", "taxes.txt"),
    ("share lyrics.txt with Dan", "lyrics.txt"),
    ('emial bob@example.com saying "txt me tmrw"', '"txt me tmrw"'),
    ("open https://example.com/txt/appt?msg=tmrw", "https://example.com/txt/appt?msg=tmrw"),
    ("emial dana@example.com the file `notes.txt`", "`notes.txt`"),
])
def test_normalizer_never_rewrites_addresses_paths_files_quotes_or_urls(text, kept):
    assert kept in normalize_typos(text)


@pytest.mark.parametrize("text", ["what does tmr stand for in medicine", "the word emial is misspelled",
                                  "what does appt mean", "how do you spell txt", "is msg short for message"])
def test_normalizer_leaves_meta_linguistic_text_unchanged(text):
    assert normalize_typos(text) == text


def _content(decision):
    return (json.dumps(decision.tool_argument_bindings, sort_keys=True, default=str),
            json.dumps(decision.direct_calls, sort_keys=True, default=str),
            decision.resolved_request)


def _route_without_normalization(text, monkeypatch, **kw):
    import service.router.normalize as N
    with monkeypatch.context() as m:
        m.setattr(N, "normalize_typos", lambda t: t)
        return _route(text, **kw)


@pytest.mark.parametrize("text", REVIEW_P1)
def test_recipients_bodies_and_args_never_come_from_the_normalized_copy(text, monkeypatch):
    assert _content(_route(text)) == _content(_route_without_normalization(text, monkeypatch))


@pytest.mark.parametrize("text", ["print report.txt", "what is in budget.txt", "trash ~/Desktop/old.txt",
                                  "zip up taxes.txt", "share lyrics.txt with Dan", "read notes.txt on my desktop"])
def test_file_names_with_txt_are_not_rerouted_by_the_normalizer(text):
    decision = _route(text)
    assert "typo-normalized" not in decision.reason
    if "share" not in text:  # sharing with a person may legitimately offer a message
        assert not _reachable(decision) & {"view_messages", "summarize_messages", "draft_message"}


def test_meta_question_about_a_shorthand_is_not_a_web_search_for_its_expansion():
    decision = _route("what does tmr stand for in medicine")
    assert not any("tomorrow" in json.dumps(args) for _, args in decision.direct_calls)
    assert "tomorrow" not in json.dumps(decision.tool_argument_bindings)


def test_ping_me_about_an_appt_is_not_narrowed_to_a_calendar_read():
    decision = _route("ping me about the appt at 3")
    assert "typo-normalized" not in decision.reason
    assert decision.reason.startswith("ambiguous")  # same retrieved route as main


_ADDRS = ["appt@clinic.org", "bob@txt.att.net", "tmrw@msg.com", "chk_txt@remeber.io", "sam+txt@appt.co"]
_PATHS = ["~/Desktop/txt", "/tmp/appt/msg.txt", "./serch.md", "C:\\\\drft\\\\pls.txt", "notes_tmrw.txt",
          "budget-2day.csv", "#txt", "v2.txt"]
_QUOTES = ['"txt me tmrw"', "'chk the calender'", "“pls remeber”", "`msg appt`"]


@pytest.mark.parametrize("verb,thing", list(itertools.product(
    ["emial", "txt", "share", "print", "open", "send"], _ADDRS + _PATHS + _QUOTES)))
def test_fuzz_protected_tokens_survive_and_content_is_identical(verb, thing, monkeypatch):
    text = f"{verb} {thing} tmrw pls"
    assert thing in normalize_typos(text)
    assert _content(_route(text)) == _content(_route_without_normalization(text, monkeypatch))


# --- P2 repair: the retrieval gate fails toward availability -----------------

OUTBOUND_REVIEW = [
    # QA R1 (F01-F12, F25, F26) and the reviewer's list
    "ask Sam if he's free for dinner", "wish mom a happy birthday", "thank Dan for the gift",
    "invite Sam to dinner on saturday", "give mom a buzz", "get in touch with the landlord", "say hi to mom",
    "write to Sam about dinner", "nudge Dan about the report", "confirm with Dan for 3pm",
    "apologize to Dan for being late", "whatsapp Sam I'm late", "hit up Sam about lunch",
    "ask mom when she lands", "contact the landlord", "answer Jane", "get back to Jane",
    "holler at Sam", "reach out to Jane about the invoice", "pass this along to Dan",
    "remind Sam that the meeting moved", "slack Dan the file", "tag Priya in it", "accept Jane's invite",
    "give dad a ring", "drop Priya a line about friday", "let Sam know I'm outside",
    "congratulate Priya on the promotion", "follow up with Jane about the invoice", "RSVP yes to Jane's party",
]


@pytest.mark.parametrize("prompt", OUTBOUND_REVIEW)
def test_requests_addressed_to_someone_keep_a_send_or_call_tool(prompt):
    reach = _reachable(_route(prompt))
    assert reach & (SENDS | {"draft_message", "draft_email"}), prompt


@pytest.mark.parametrize("prompt,ctx", [
    ("try again", dict(last_user="text Sam I'm late", last_assistant="I couldn't send that message.",
                       last_tools="send_message")),
    ("Bob as well", dict(last_user="email Sam the agenda", last_assistant="Sent the agenda to Sam.",
                         last_tools="send_email")),
])
def test_continuing_an_outbound_turn_keeps_the_send_tools(prompt, ctx):
    assert _reachable(_route(prompt, **ctx)) & SENDS


@pytest.mark.parametrize("prompt", ["destroy the old files", "ditch the past reminders", "dump all my memories",
                                    "blow away the old logs folder", "nuke my reminders",
                                    "delet my old reminders", "clar all reminders"])
def test_delete_paraphrases_and_typos_open_the_bulk_delete_gate(prompt):
    assert any(semantic.gated_tool_allowed(t, prompt) for t in BULK_DELETES)


@pytest.mark.parametrize("prompt", ["did i have a meeting with Priya last week",
                                    "pencil in coffee with Jordan next wednesday at 9",
                                    "what's the group chat saying", "archive them",
                                    "write a toast for my sister's wedding", "what's mom's birthday dinner",
                                    "give me a polite way to say no to a meeting"])
def test_mentions_of_people_that_address_nobody_stay_non_outbound(prompt):
    assert not semantic.outbound_intent(prompt)
