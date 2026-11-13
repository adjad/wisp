"""0.5.0: the tool an everyday request names leads Ask's head start.

A 2026-11-12 probe of demo-style questions found wordings whose tool was not
among the six real schemas the model is handed first: "what did mom say" led
with lookup_contact and travel_time, "set a timer for 5 minutes" with
plan_meeting, "email Dana the notes" offered no send_email at all. The model
could still find the tool through discovery, at the cost of a step it often
skipped. These rules only order tools the turn already has; they open none.
"""
from __future__ import annotations

import pytest

from service import main
from service import model_only as mo
from service.router.router import routing_guard_contract

LEADS = [
    ("what did mom say", "view_messages"), ("did mom reply", "view_messages"),
    ("did Dana text me back", "view_messages"), ("what did Sam say about the project", "view_messages"),
    ("any texts from Dana", "view_messages"), ("who texted me today", "view_messages"),
    ("any new texts", "view_messages"), ("did Sam email me", "view_emails"), ("any grade updates", "view_emails"),
    ("email Dana the notes", "send_email"), ("can you email Sam that I'm late", "send_email"),
    ("set a timer for 5 minutes", "set_timer"), ("start a 10 minute timer", "set_timer"),
    ("set an alarm for 7am", "set_timer"), ("do I have any reminders", "search_reminders"),
    ("what's overdue", "search_reminders"), ("how did the S&P 500 move today", "get_stock_price"),
]
NO_LEAD = [
    "what did you say", "what did the president say today", "did you send it", "did it rain yesterday",
    "what does this say", "email me the notes", "what is the email address for Dana", "did the Warriors win",
    "what's on my calendar today", "Is Costco open on Sunday?", "text mom I'm running late",
    "remind me to call Sam at 5", "has the package shipped", "what's the weather tomorrow", "hey wisp",
]


@pytest.mark.parametrize("prompt,tool", LEADS)
def test_the_named_tool_leads_the_head_start(prompt, tool):
    assert mo.direct_leads(prompt)[:1] == [tool]
    preload, _ = main._ask_tool_rank(prompt)
    assert preload[:1] == [tool], preload


@pytest.mark.parametrize("prompt", NO_LEAD)
def test_other_requests_keep_their_own_order(prompt):
    assert mo.direct_leads(prompt) == []


def test_a_lead_is_only_an_order_and_never_a_permission():
    # The guard decides what a turn may use; a lead it forbids is simply not offered.
    for prompt, tool in LEADS:
        forbidden_before, _ = routing_guard_contract(prompt, model_led=True)
        main._ask_tool_rank(prompt)
        forbidden_after, _ = routing_guard_contract(prompt, model_led=True)
        assert set(forbidden_before) == set(forbidden_after), prompt


def test_long_or_non_ascii_text_gets_no_lead():
    assert mo.direct_leads("did mom reply " + "and also this " * 12) == []
    assert mo.direct_leads("did mоm reply") == []            # a Cyrillic o


def test_a_public_question_still_leads_with_the_search():
    assert main._ask_tool_rank("who is the president?")[0][:1] == ["web_search"]
    assert main._ask_tool_rank("what's on my calendar today")[0][:1] == ["get_upcoming"]
