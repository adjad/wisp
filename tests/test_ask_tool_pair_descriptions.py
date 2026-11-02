"""The most-confused Ask tool pairs (toolbench 2026-10-31, wrong_tool /
unoffered_tool / hallucinated_tool across Gemma and Bolt runs): each
description says when to use the rival instead, and sharpening them did not
grow the schema budget (tool schemas are ~60% of a 16k window)."""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-ask-desc-"))

import pytest  # noqa: E402

import service.tools  # noqa: E402,F401
from service.tools.registry import REGISTRY  # noqa: E402

# (tool, rival its description must name)
PAIRS = [
    ("send_email", "draft_email"), ("send_email", "schedule_send"),
    ("draft_email", "send_email"),
    ("send_message", "draft_message"), ("send_message", "schedule_send"),
    ("schedule_send", "send_message"), ("schedule_send", "send_email"),
    ("lookup_contact", "send_message"),
    ("view_messages", "summarize_messages"), ("summarize_messages", "view_messages"),
    ("search_conversations", "view_messages"),
    ("web_search", "get_stock_price"), ("web_fetch", "web_search"), ("get_stock_price", "web_search"),
    ("search_reminders", "update_reminder"), ("cancel_event", "update_event"),
    ("cancel_event", "clear_reminders"), ("update_event", "update_reminder"),
    ("update_reminder", "update_event"), ("add_reminder", "add_calendar_event"),
    ("add_reminder", "create_note"), ("create_note", "add_reminder"),
    ("get_upcoming", "get_my_day_plan"),
    ("clear_reminders", "manage_timers"), ("manage_timers", "clear_reminders"),
]


@pytest.mark.parametrize("tool,rival", PAIRS)
def test_each_confused_tool_names_its_rival(tool, rival):
    assert rival in REGISTRY[tool].description


def test_a_plain_email_request_is_a_send_and_crypto_is_a_search():
    assert "is a send, not a draft" in REGISTRY["send_email"].description
    assert "Prefer this over send_email" not in REGISTRY["draft_email"].description
    assert "crypto" in REGISTRY["web_search"].description
    assert "CoinGecko form instead" not in REGISTRY["get_stock_price"].description


def test_lookup_contact_is_no_longer_a_required_step_before_a_send():
    assert "Use this before send_message" not in REGISTRY["lookup_contact"].description
    assert "resolved automatically" in REGISTRY["send_message"].description


def test_schedule_send_says_now_is_an_immediate_send():
    text = REGISTRY["schedule_send"].description
    assert "If they say now" in text and "send_message" in text


def test_forget_states_its_narrow_scope():
    text = REGISTRY["forget"].description
    assert "one named fact" in text and "everything from mail" in text


def test_sharpened_descriptions_did_not_grow_the_schema_budget():
    # 40,605 characters across 93 built-in tools before this change.
    builtin = [t for t in REGISTRY.values() if t.category not in {"skill_tool", "mcp_read", "mcp_action"}]
    assert sum(len(t.description or "") for t in builtin) <= 40_605


def test_retrieval_text_stays_pinned_so_rankings_do_not_move():
    from service.tools.retrieval_pins import PINS
    for name, text in PINS.items():
        tool = REGISTRY[name]
        assert tool.effective_retrieval_description == text
        assert tool.description != text      # the model-facing text did change
