"""Structural routing fixes measured on the routing-quality corpus.

See docs/ROUTING_DIAGNOSIS.md. Router only: no model, embedding server, tool
body or network. Retrieval uses the packaged lexical provider (and the stub
embedder, should a configuration select the embedding provider).
"""
import asyncio

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
