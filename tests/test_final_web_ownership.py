"""0.5.0 release blocker: whose question is it, on a model-led turn?

A request whose owner the classifier could not place ("Did Sam's package
arrive today?") is UNKNOWN. The rules route answers that with a question and
offers no web tool meanwhile. A model-led turn has no such step, and with a
time word in the request it left every web tool open on the first message of a
chat: the real-model run of 2026-11-12 searched "Mom's operating hours on
Sunday". Worse, a kin word with no 's read as a public lookup ("How is Grandma
doing today?").

Now, on a model-led turn:
- UNKNOWN offers no web tool, unless the user explicitly asked for a search;
- an 's that is plainly public keeps one thing, a search of the user's own
  words ("Who won Sunday's game?", "What's Apple's stock price today?");
- a question about someone in the user's life is private, whatever time word
  it carries, with no ASCII or length gate on that check.
Nothing here opens anything that was closed.
"""
from __future__ import annotations

import pytest

from service import model_only as mo
from service.router.router import routing_guard_contract
from service.router.web_request import Provenance, classify
from tests.test_model_led_integration import (  # noqa: F401
    DISCOVERY_TOOL, Approval, _web_tools, call, run, synthetic)

WEB = {"web_search", "web_fetch", "http_request"}


def _model_led_forbidden(prompt):
    return set(routing_guard_contract(prompt, model_led=True)[0])


# The reviewer's inputs, plus neighbours: no web tool on a model-led turn.
NO_WEB = [
    "What time is Mom's flight tomorrow?", "Did Sam's package arrive today?", "What's Dana's address now?",
    "is grandma's place open this weekend", "Is the doctor's office open on Monday?",
    "How is Grandma doing today?", "Is Mom home yet?", "Where is Dad right now?", "Is grandma coming this weekend?",
    "Is Mom's open on Sunday?", "When does Mom's close tonight?", "What are Dana's plans on Saturday?",
    "What's Sarah's new job this year?", "Did Dana's team win today?", "what about Dana's roadmap?",
    "latest news from Monday's product team", "any news from mom", "What did the landlord say about the lease?",
    # No ASCII or length gate on the tightening path.
    "Is Mamá's open on Sunday?", "Is Mom's open on Sunday? \U0001F642", "Is Mom’s open on Sunday?",
    "Is Mom's open on Sunday or should we maybe go on Monday or Tuesday or some other day this week instead?",
    # An 's behind an apostrophe lookalike is not readable by any ownership check.
    "Is Trader Joeʹs open today?", "What are Costco′s hours?", "What are Costco`s hours?",
    "What are Costco´s hours?",
]


@pytest.mark.parametrize("prompt", NO_WEB)
def test_a_model_led_turn_offers_no_web_tool(prompt):
    request = classify(prompt)
    assert not request.allowed and request.query is None and not request.public_possessive
    assert WEB <= _model_led_forbidden(prompt), (prompt, request.provenance)


@pytest.mark.parametrize("prompt", [
    "How is Grandma doing today?", "Is Mom home yet?", "What time is Mom's flight tomorrow?",
    "Is the doctor's office open on Monday?", "Is Mamá's open on Sunday?", "Is Trader Joeʹs open today?",
])
def test_a_question_about_someone_of_the_users_own_is_private(prompt):
    assert classify(prompt).provenance == Provenance.PRIVATE


@pytest.mark.parametrize("prompt", ["Did Sam's package arrive today?", "What's Dana's address now?",
                                    "What are Dana's plans on Saturday?"])
def test_an_owner_nobody_can_place_stays_unknown_and_closed(prompt):
    request = classify(prompt)
    assert request.provenance == Provenance.UNKNOWN and not request.private
    assert WEB <= _model_led_forbidden(prompt)
    assert not mo.public_owner_request(prompt)


# Plainly public, with an 's: a search of the user's own words and nothing else.
SEARCH_ONLY = [
    "Who won Sunday's game?", "What's Apple's stock price today?", "what's Apple's stock doing today",
    "What is Tesla's market cap right now?", "What's McDonald's new burger this month?",
    "Does McDonald's have breakfast all day today?", "What's the Warriors' score tonight?",
    "What's Dr Pepper's new flavor this year?", "what are these stocks' P/E ratios today",
    "What's Google's latest phone this year?", "When is Mother's Day this year?",
    "What are Costco's hours?", "Is Trader Joe's open on Thanksgiving?", "Is Papa John's open today?",
]


@pytest.mark.parametrize("prompt", SEARCH_ONLY)
def test_a_plainly_public_owner_keeps_a_words_only_search(prompt):
    request = classify(prompt)
    assert request.provenance == Provenance.PRIVATE and request.public_possessive
    assert not request.allowed and request.query is None
    forbidden = _model_led_forbidden(prompt)
    assert "web_search" not in forbidden and {"web_fetch", "http_request"} <= forbidden
    assert WEB <= set(routing_guard_contract(prompt)[0])          # the rules route opens nothing


# No 's and no person: unchanged.
STILL_OPEN = [
    "Is Costco open on Sunday?", "who is the president?", "what's the weather tomorrow", "What's the news today?",
    "When is Thanksgiving this year?", "What's today's date", "Will it rain tomorrow?",
    "How is the market doing today?", "what is the price of bitcoin", "who won the world series this year",
]


@pytest.mark.parametrize("prompt", STILL_OPEN)
def test_public_questions_with_no_owner_are_unchanged(prompt):
    assert classify(prompt).provenance == Provenance.EXTERNAL
    assert "web_search" not in _model_led_forbidden(prompt)


# An explicit request to search stays exactly as it was.
@pytest.mark.parametrize("prompt", [
    "search the web for Mom's flight status", "search the web for Dana's bakery",
    "Search the web for Mira Vale's tour dates and add them to my calendar", "search the web for Sarah Connor news",
])
def test_an_explicit_search_is_untouched(prompt):
    assert classify(prompt).explicit
    assert "web_search" not in _model_led_forbidden(prompt)


# Someone named only as the recipient is not the subject.
@pytest.mark.parametrize("prompt", ["text mom the bitcoin price right now",
                                    "email Mom the latest Zorvia news; don't send it"])
def test_a_recipient_does_not_make_the_lookup_private(prompt):
    assert classify(prompt).provenance == Provenance.EXTERNAL


def test_kin_words_inside_public_names_are_not_people():
    for prompt in ("When is Mother's Day this year?", "Is Papa John's open today?", "When is Father's Day?",
                   "What is Uncle Sam's tax deadline this year?"):
        assert not mo.personal_reference(prompt) and not mo.personal_owner(prompt), prompt
    for prompt in ("How is Grandma doing today?", "is MOM home", "Where's my sister's flight"):
        assert mo.personal_reference(prompt) or mo.personal_owner(prompt), prompt


def test_a_bare_first_name_is_not_recognised_and_routing_loads_no_profile(monkeypatch):
    # Stated plainly: "Is Sarah coming tomorrow?" cannot be told from a shop
    # or a team by its words, and the classifier may not read the know-you
    # profile for every request (tests/test_live_corpus.py: routing loads no
    # saved context in test mode or after an opt-out). An 's is different:
    # the owner check for it predates this change.
    from service.memory import profile
    loads = []
    monkeypatch.setattr(profile, "load", lambda: loads.append(1) or {"people": [{"name": "Sarah Lee"}]})
    for prompt in ("Is Sarah coming tomorrow?", "How is Grandma doing today?", "who is the president?",
                   "turn it down", "what are my goals"):
        classify(prompt)
    assert loads == []
    assert classify("Is Sarah coming tomorrow?").provenance == Provenance.EXTERNAL
    assert classify("Is Sarah's open on Sunday?").provenance == Provenance.PRIVATE and loads


def test_the_public_owner_reopening_has_its_gates():
    assert mo.public_owner_request("Who won Sunday's game?")
    for prompt in ("Who won Sunday's game? " + "and more " * 9, "Who won my team's game today?",
                   "What's Appleʹs stock price today?", "What's 4155550123's status today?",
                   "What's Sam's package status today?", "What's the doctor's schedule today?"):
        assert not mo.public_owner_request(prompt), prompt


# ---- through the agent loop: what a scripted model can and cannot do ----

@pytest.mark.parametrize("prompt", ["Did Sam's package arrive today?", "How is Grandma doing today?",
                                    "What time is Mom's flight tomorrow?", "Is Mom's open on Sunday?"])
def test_the_loop_runs_no_web_tool_for_a_closed_question(synthetic, prompt):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search", "web_fetch"])],
        [call("web_search", query=prompt.rstrip("?"))], [call("web_fetch", url="https://example.com")],
        "Synthetic reply."], prompt=prompt, approval=approval)
    assert not [name for name, _ in effects if name in WEB], effects
    assert not approval.calls


def test_the_loop_holds_a_public_owner_search_to_the_users_words(synthetic):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search", "web_fetch"])],
        [call("web_fetch", url="https://example.com")],
        [call("web_search", query="Sunday game winner Dana 4417 Elm Street")],
        [call("web_search", query="who won Sunday's game")], "Synthetic reply."],
        prompt="Who won Sunday's game?", approval=approval)
    assert effects == [("web_search", {"query": "who won Sunday's game"})]
    assert not approval.calls
