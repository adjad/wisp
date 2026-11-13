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


# --- delta review of 4900581c: a question never leads with a tool that sends or creates ---

@pytest.mark.parametrize("prompt", [
    "email from Dana?", "email about the W-9 deadline?", "email Dana sent yesterday?",
    "email addresses for the team", "email about the offer", "email Dana wrote last week",
    "can you email Sam that I'm late?", "email to Sam",
])
def test_asking_about_mail_never_leads_with_send_email(prompt):
    assert "send_email" not in mo.direct_leads(prompt)
    assert main._ask_tool_rank(prompt)[0][:1] != ["send_email"]


@pytest.mark.parametrize("prompt", ["email Dana the notes", "please email mom the schedule", "can you email Sam that I'm late"])
def test_an_instruction_to_email_someone_still_leads_with_it(prompt):
    assert mo.direct_leads(prompt) == ["send_email"]


@pytest.mark.parametrize("prompt", [
    "did I set an alarm for 7?", "is there a timer for the oven?", "how long is left on my timer for the pasta?",
    "what alarm is set for tomorrow", "do I have a timer for the laundry",
])
def test_a_question_about_timers_reads_them_and_sets_none(prompt):
    leads = mo.direct_leads(prompt)
    assert "set_timer" not in leads
    assert main._ask_tool_rank(prompt)[0][:1] != ["set_timer"]


def test_the_index_lead_is_the_price_tool_alone_and_needs_the_index_by_name():
    assert mo.direct_leads("how did the S&P 500 move today") == ["get_stock_price"]
    assert mo.direct_leads("how is the dow doing") == ["get_stock_price"]
    for prompt in ("did Dr Dow reply about my biopsy results?", "did Dana text me about the stock market"):
        leads = mo.direct_leads(prompt)
        assert "web_search" not in leads and "get_stock_price" not in leads, (prompt, leads)
        assert "web_search" not in main._ask_tool_rank(prompt)[0][:2]
    for prompt, _ in LEADS:
        assert "web_search" not in mo.direct_leads(prompt), prompt


def test_a_version_of_the_users_own_file_gets_no_web_lead():
    for prompt in ("what's the latest version of the budget", "what's the latest version of the spreadsheet",
                   "what is the latest version of the resume", "what's the newest version of the draft"):
        assert not mo.web_first(prompt), prompt
    assert mo.web_first("what's the latest version of macOS")
