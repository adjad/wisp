"""0.5.0: fewer dead ends for public web questions on the first message.

rc2 knew "What are Costco's hours?" only in that wording. A business name
with 's asked as "Is ... open", "When does ... close" or "How late is ... open"
stayed private and got no search. Those wordings now follow the same rule:
web_search only, words-only query, never web_fetch or http_request, and never
for a relation, a contact detail or a person Wisp knows.

Separately, a public fact that changes ("Is Costco open on Sunday?", "who is
the president?") was already allowed to search, but web_search was not in the
Ask head start and the model answered from memory (2026-11-04 debug export:
"who is the president?" answered with no tool). It leads the head start now.
Nothing here opens a tool a turn did not already have.
"""
from __future__ import annotations

import pytest

from service import main
from service import model_only as mo
from service.router.router import routing_guard_contract
from service.router.web_request import Provenance, classify

WEB = ("web_search", "web_fetch", "http_request")
OPEN_WORDINGS = [
    "Is Trader Joe's open on Thanksgiving?", "When does Costco's pharmacy close?", "How late is Lowe's open?",
    "Is McDonald's open right now?", "What time does Kohl's open tomorrow?", "is wendy's still open",
    "When does Macy's close tonight?", "Is Trader Joe’s open on Sundays?", "Are Sam's open today?",
    "What time does Costco's gas station open?", "Is Denny's open late?",
]
STILL_PRIVATE = [
    "Is Mom's open on Sunday?", "When does my dentist's close?", "is her boss's store open today",
    "When does Sarah's flight close?", "Is Trader Joe's open and text Sam", "Is 4155550123's open?",
    "Is 12 Elm St's open?", "What is Costco's phone number?", "When does Dana's shift close?",
    "Is Trader Joe's open on Thanksgiving? My gate code is 4471.", "Is Trader Joeʹs open today?",
]


@pytest.mark.parametrize("prompt", OPEN_WORDINGS)
def test_open_and_close_wordings_are_public_possessive_questions(prompt):
    assert mo.public_possessive_question(prompt) and mo.public_fact_question(prompt)


@pytest.mark.parametrize("prompt", OPEN_WORDINGS)
def test_they_stay_private_for_egress_and_open_only_a_search(prompt):
    request = classify(prompt)
    assert request.provenance == Provenance.PRIVATE and request.private and request.public_possessive
    assert request.query is None and not request.allowed
    forbidden, _ = routing_guard_contract(prompt, model_led=True)
    assert "web_search" not in forbidden and {"web_fetch", "http_request"} <= set(forbidden)
    forbidden, _ = routing_guard_contract(prompt)            # the rules route opens nothing
    assert set(WEB) <= set(forbidden)


@pytest.mark.parametrize("prompt", STILL_PRIVATE)
def test_personal_or_compound_wordings_get_no_search(prompt):
    request = classify(prompt)
    assert not mo.public_possessive_question(prompt) and not request.public_possessive
    # None of them may search on a model-led turn: each is private, or of unknown ownership.
    forbidden, _ = routing_guard_contract(prompt, model_led=True)
    # (http_request is absent from the set when a rule forbids "every tool but these".)
    assert {"web_search", "web_fetch"} <= set(forbidden), (prompt, request.provenance)


# The same wordings about someone of the user's own. With a time word the
# classifier left these of unknown ownership, where a model-led turn could
# still search them: the real-model run of 2026-11-12 searched "Mom's operating
# hours on Sunday". The earlier form of the test above passed without checking.
OWN_PERSON = [
    "Is Mom's open on Sunday?", "When does Mom's close tonight?", "What are Mom's hours today?",
    "How late is Dad's open?", "Is my dentist's open today?", "is her boss's store open today",
    "What time does Grandma's open tomorrow?", "Is the doctor's open now?", "When does Dad's close on Sundays?",
]


@pytest.mark.parametrize("prompt", OWN_PERSON)
def test_an_own_persons_hours_are_private_on_every_route(prompt):
    request = classify(prompt)
    assert request.provenance == Provenance.PRIVATE and request.private
    assert not request.public_possessive and not request.allowed and request.query is None
    for model_led in (True, False):
        forbidden, _ = routing_guard_contract(prompt, model_led=model_led)
        assert set(WEB) <= set(forbidden), (prompt, model_led)
    assert not mo.web_first(prompt) and main._ask_tool_rank(prompt)[0][:1] != ["web_search"]


def test_a_known_persons_hours_are_private_too(monkeypatch):
    monkeypatch.setattr(mo, "_known_people_tokens", lambda: {"dana", "lee"})
    for prompt in ("Is Dana's open on Sunday?", "When does Dana Lee's close tonight?"):
        assert mo.personal_possessive_question(prompt)
        forbidden, _ = routing_guard_contract(prompt, model_led=True)
        assert set(WEB) <= set(forbidden), prompt
    assert not mo.personal_possessive_question("Is Trader Joe's open on Sunday?")
    assert not mo.personal_possessive_question("Is Costco open on Sunday?")       # no 's: not this shape


def test_a_known_person_is_not_a_business(monkeypatch):
    monkeypatch.setattr(mo, "_known_people_tokens", lambda: {"dana", "lee"})
    assert not mo.public_possessive_question("Is Dana's open today?")
    assert not mo.web_first("Is Dana open on Sunday?")
    assert mo.public_possessive_question("Is Trader Joe's open today?")

    def broken():
        raise OSError("unreadable profile")
    monkeypatch.setattr(mo, "_known_people_tokens", broken)
    assert not mo.public_possessive_question("Is Trader Joe's open today?")     # fails closed
    assert not mo.web_first("Is Costco open on Sunday?")


def test_the_search_query_is_still_held_to_the_users_words():
    asked = "Is Trader Joe's open on Thanksgiving?"
    assert mo.public_fact_query_problem("Trader Joe's Thanksgiving hours", asked) is None
    assert mo.public_fact_query_problem("Trader Joe's Thanksgiving hours Springfield Dana", asked)


WEB_FIRST = [
    "Is Costco open on Sunday?", "Is Costco open today?", "When does Target close?", "What time does Target open tomorrow?",
    "How late is Walmart open?", "Costco hours", "Costco hours Sunday", "what time does the library close today",
    "Is the pool open today?", "who is the president?", "Who is the president of the United States?",
    "Who is the CEO of Apple?", "who is the governor of California", "what is the latest iPhone",
    "what's the latest version of macOS", "What's the news today?", "Is Trader Joe's open on Thanksgiving?",
]
NOT_WEB_FIRST = [
    "who is my manager", "is mom open to visiting", "what are my office hours", "When does my class close",
    "who is the president of my club", "what's the latest on my order", "office hours with TA today",
    "when does the meeting open", "who is the teacher", "is the doctor open saturday",
    "what is the latest email from Sam", "what is the latest message", "any news from mom",
    "is the garage door open", "is the front door closed", "what is the latest reminder",
    "what's the latest from Dana", "Is Dana open to it?", "what's on my calendar today",
    "What is the capital of France?", "how should i spend my day tomorrow",
    "Is Costco open on Sunday and can you text Sam the answer and remind me to go and buy milk there",
    # Review of c32e4686: a topic of the user's own after "latest" or "news", or a thing that is "open".
    "what's the latest on the Henderson deal", "any news on the job application?", "is the ticket still open?",
    "is the deal closed?", "what's the latest on Project Falcon", "any news about the offer",
    "is the case still open", "is the claim closed", "what is the latest status", "latest news about NVIDIA",
]


@pytest.mark.parametrize("prompt", WEB_FIRST)
def test_a_public_fact_that_changes_leads_with_the_search(prompt):
    assert mo.web_first(prompt)
    preload, _ = main._ask_tool_rank(prompt)
    assert preload[:1] == ["web_search"], preload


@pytest.mark.parametrize("prompt", NOT_WEB_FIRST)
def test_the_users_own_things_never_lead_with_the_search(prompt):
    assert not mo.web_first(prompt)
    preload, _ = main._ask_tool_rank(prompt)
    assert preload[:1] != ["web_search"], preload


def test_the_head_start_opens_nothing_by_itself():
    # web_first only orders tools the turn already has; the guard is unchanged.
    for prompt in ("Is Costco open on Sunday?", "who is the president?"):
        before = classify(prompt)
        assert before.provenance == Provenance.EXTERNAL and not before.private
    private = "what is the latest email from Sam"
    forbidden, _ = routing_guard_contract(private, model_led=True)
    assert not mo.web_first(private)
    assert main._ask_tool_rank(private)[0][:1] != ["web_search"]


def test_mail_and_calendar_leads_still_win():
    for prompt in ("When is the W-9 due?", "When is the next meeting?"):
        preload, _ = main._ask_tool_rank(prompt)
        assert preload[:1] != ["web_search"], (prompt, preload)


def test_the_classifier_does_not_protect_a_private_topic_so_the_pattern_must():
    # classify() calls these external and leaves web_search allowed; only the
    # narrow web_first pattern keeps the search from leading.
    for prompt in ("what's the latest on the Henderson deal", "any news on the job application?",
                   "is the ticket still open?", "is the deal closed?"):
        request = classify(prompt)
        assert request.provenance == Provenance.EXTERNAL and not request.private
        assert not mo.web_first(prompt) and main._ask_tool_rank(prompt)[0][:1] != ["web_search"]


def test_latest_and_news_lead_only_for_products_and_the_days_news():
    for prompt in ("what is the latest iPhone", "what's the latest version of macOS", "what's the latest macbook",
                   "What's the news today?", "top headlines", "what is the news"):
        assert mo.web_first(prompt), prompt
    for prompt in ("what's the latest", "what is the latest on that", "any news?", "news about the merger",
                   "what's the latest version of the contract"):
        assert not mo.web_first(prompt), prompt


def test_a_known_first_name_makes_a_longer_owner_personal(monkeypatch):
    monkeypatch.setattr(mo, "_known_people_tokens", lambda: {"dana", "smith", "joe"})
    assert not mo.public_possessive_question("What are Dana Lee's hours?")       # a Dana is known
    assert not mo.public_possessive_question("Is Dana Lee's open today?")
    assert mo.public_possessive_question("What are Trader Joe's hours?")         # Joe known, but not first
    assert mo.public_possessive_question("Is Costco's pharmacy open today?")
