"""The router's keyword tier.

`rule_route` is the fast path that decides, without a model call, which expert
handles a request and — the high-stakes part — whether it `needs_tools`. Only a
request that needs tools reaches the agent loop, which is the only place tools
actually run.

The rule set is designed to fail SAFE: an unmatched prompt falls through to the
tool-capable model rather than to the tool-less fast one. These tests pin the
cases the rules were written to catch, plus the safety case the comments in
router.py call out explicitly.
"""
from __future__ import annotations

import pytest

from service.router.router import rule_route


def _route(text: str, has_image: bool = False):
    return rule_route(text, has_image)


# --------------------------------------------------------------------------
# Tool intent — the decision that matters most
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    "quit safari",
    "play some music",
    "check my calendar",
    "summarize my emails",
    "what's in my inbox",
    "cancel my lunch with sam",
    "when is my next meeting",
    "do I have any meetings today",
    "remind me to call mom at 4pm",
    "add a dentist appointment friday at 2pm",
    "look at my screen and tell me what app is in focus",
    "what am I looking at",
    "take a screenshot",
    "delete the temp files",
    "run a script to list files in my downloads",
])
def test_operating_the_machine_needs_tools(prompt):
    d = _route(prompt)
    assert d is not None, f"expected a rule to match {prompt!r}"
    assert d.needs_tools is True


def test_greeting_plus_destructive_action_is_not_trivial():
    """The safety case router.py calls out: a friendly opener must not get the
    whole request classified as chit-chat for the tool-less fast model."""
    d = _route("hello can you delete all my files")
    assert d is not None
    assert d.needs_tools is True
    assert d.role != "fast"


@pytest.mark.parametrize("prompt", [
    "write a python function to sort a list",
    "fix the bug in my palindrome function",
])
def test_pure_code_generation_does_not_need_tools(prompt):
    d = _route(prompt)
    assert d is not None
    assert d.role == "coding"
    assert d.needs_tools is False


@pytest.mark.parametrize("prompt", [
    "prove that sqrt 2 is irrational",
    "find the derivative of x^2",
])
def test_hard_reasoning_routes_to_reasoning(prompt):
    d = _route(prompt)
    assert d is not None
    assert d.role == "reasoning"
    assert d.needs_tools is False


@pytest.mark.parametrize("prompt", ["hello there", "thanks bud", "good morning"])
def test_trivial_chitchat_routes_to_fast(prompt):
    d = _route(prompt)
    assert d is not None
    assert d.role == "fast"
    assert d.needs_tools is False


# --------------------------------------------------------------------------
# Vision
# --------------------------------------------------------------------------

def test_an_attached_image_forces_vision():
    d = _route("what is this", has_image=True)
    assert d is not None
    assert d.role == "vision"
    assert d.needs_vision is True


# --------------------------------------------------------------------------
# Falling through
# --------------------------------------------------------------------------

def test_genuinely_ambiguous_prompt_falls_through_to_the_llm_tier():
    """"make it vegetarian" is a follow-up with no standalone signal. The rules
    are supposed to decline rather than guess — the LLM classifier handles it."""
    assert _route("make it vegetarian") is None


def test_a_matched_decision_names_its_reason():
    d = _route("quit safari")
    assert d is not None
    assert d.reason
    assert d.source == "rules"
