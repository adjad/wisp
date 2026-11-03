"""Reply drafts read the conversation first, and day corrections stay day plans.

2026-11-02 r3: "draft a message responding to trishe" counted as pure writing
(_STORED_CONTENT and _DESTINATION knew only "reply/respond to"), so Ask
withheld view_messages AND draft_message and the model answered from nothing.
Every form of replying now counts, read tools lead the head start, and plain
writing that only mentions a "response" stays plain. Registry and regex
checks only: no model, store or network.
"""
from __future__ import annotations

import re

import pytest

from service import main
from service import model_only as mo
from service.tools.registry import REGISTRY

TRISHE = "draft a message responding to trishe"


@pytest.mark.parametrize("prompt", [
    TRISHE, "draft a message replying to trishe", "Draft a reply to Trishe", "write a response to Mom's text",
    "write a response to mom", "draft a response to Trishe", "write something that responds to her email",
    "draft an answer to my professor", "compose an answer to Ed", "write a quick text answering her",
    "can you draft replies to the group chat", "draft a message to respond to Avika"])
def test_every_form_of_replying_keeps_reads_and_the_draft(prompt):
    assert not mo.pure_writing(prompt)
    assert not mo.compose_only(prompt)
    forbidden = mo.ask_forbidden_tools(prompt, REGISTRY)
    assert not {"view_messages", "view_emails", "draft_message", "draft_email"} & forbidden


@pytest.mark.parametrize("prompt", [TRISHE, "draft a message replying to trishe", "Draft a reply to Trishe"])
def test_the_message_read_leads_before_the_draft(prompt):
    preload, _ = main._ask_tool_rank(prompt)
    assert preload[0] == "view_messages"
    assert "draft_message" in preload and preload.index("view_messages") < preload.index("draft_message")


def test_a_reply_by_text_leads_with_messages():
    assert main._ask_tool_rank("write a quick text answering her")[0][0] == "view_messages"


def test_an_email_reply_reads_mail_and_an_unnamed_one_reads_both():
    assert mo.reply_reads("draft an email replying to Ed") == ["view_emails"]
    assert mo.reply_reads("draft a response to Trishe") == ["view_messages", "view_emails"]
    preload, _ = main._ask_tool_rank("draft an email replying to Ed")
    assert preload[0] == "view_emails"


@pytest.mark.parametrize("prompt", [
    "write an essay on the public response to the policy", "write a poem about the answer to life",
    "write a short thank-you note to my professor", "write me a haiku about answering machines",
    "draft a LinkedIn post about my new job"])
def test_plain_writing_stays_plain(prompt):
    assert mo.pure_writing(prompt)
    assert mo.reply_reads(prompt) == []


@pytest.mark.parametrize("text", ["what's the response to the survey", "share my screen", "answering machine",
                                  "the answer to life is 42", "response time was slow"])
def test_reply_pattern_does_not_over_trigger(text):
    assert not re.search(mo._REPLY_TO, text, re.I)
    assert mo.reply_reads(text) == []


# --- day-plan corrections ----------------------------------------------------------------

@pytest.mark.parametrize("prompt", ["actually saturday", "no, saturday", "i meant tomorrow", "no wait, saturday",
                                    "actually, this week", "I meant friday?", "and saturday?"])
def test_day_corrections_follow_a_day_question(prompt):
    assert mo.day_plan_question(prompt, "what's on friday")
    assert not mo.day_plan_question(prompt, "what's the weather friday")
    assert not mo.day_plan_question(prompt)


@pytest.mark.parametrize("prompt", ["no, I want pizza", "actually never mind", "no",
                                    "actually saturday is fine for the meeting"])
def test_other_corrections_are_not_day_follow_ups(prompt):
    assert not mo.day_plan_question(prompt, "what's on friday")
