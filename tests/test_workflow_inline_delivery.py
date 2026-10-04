"""'Summarize X and text/email it to Y' binds 'it' to the read in the same sentence.

Regression: every such request ended in "I need a new delivery request with an
exact content scope" (deliver_summary workflows: 1 completed of ~150), because
the compiler treated any 'it' as unseen earlier content and counted the verb
'text' as a messages source.
"""
import pytest

from service.workflows.compiler import compile_new

CLEAN = [
    ("summarize my emails and text it to Mom", ["email"], "messages", "Mom", ""),
    ("summarize my unread emails and email it to sam@example.com", ["email"], "email", "sam@example.com", ""),
    ("summarize my messages and text it to Mom", ["messages"], "messages", "Mom", ""),
    ("what's on my calendar tomorrow and text it to Dad", ["calendar"], "messages", "Dad", "tomorrow"),
    ("check my unread emails and email them to me", ["email"], "email", "me", ""),
    ("please summarize my emails and then text it to Mom", ["email"], "messages", "Mom", ""),
    ("summarize my emails and text Mom a summary", ["email"], "messages", "Mom", ""),
]

STRICT = [
    "summarize my emails and redact the names and text it to Mom",   # unsupported transformation
    "summarize my emails from Priya and text it to Mom",              # sender filter: not supported
    "summarize that and text it to Mom",                              # 'that' is an unseen referent
    "summarize my emails and text it to Mom via imessage",            # channel phrase, not understood
    "text it to Mom",                                                 # no read at all
    "summarize my emails and delete them and text it to Mom",         # extra action
    "summarize my emails and shorten it and text it to Mom",
]


@pytest.mark.parametrize("prompt,sources,channel,recipient,date", CLEAN)
def test_same_sentence_delivery_compiles(prompt, sources, channel, recipient, date):
    plan = compile_new(prompt)
    assert plan is not None and not plan.content_error, prompt
    assert (plan.sources, plan.channel, plan.recipient, plan.date_range) == (sources, channel, recipient, date)


@pytest.mark.parametrize("prompt", STRICT)
def test_anything_not_fully_understood_still_fails_closed(prompt):
    plan = compile_new(prompt)
    assert plan is None or plan.content_error, prompt


def test_the_verb_text_is_not_a_messages_source():
    plan = compile_new("summarize my emails and text it to Mom")
    assert "messages" not in plan.sources
