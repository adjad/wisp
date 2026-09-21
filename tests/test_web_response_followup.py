"""Regression coverage for the 2026-09-20 web-response debug sequence."""
from __future__ import annotations

import tempfile
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime

from service.memory.store import SessionStore
from service.router.router import _LING_WEB_MODEL
from service.tools.web_tools import dated_news_digest
from service.workflows.engine import finish_workflow, prepare_turn
from service.workflows.reads import adjacent_stock_response, compile_read


_STOCK_RESPONSE = "GOOGL, MU, VICOR, and VOO each moved during today's session."


def test_current_news_reaches_ling_instead_of_structured_raw_read():
    assert compile_read("What is on the news today?") is None
    assert _LING_WEB_MODEL == "Ling-3.0-tiny-oQ6e"


def test_news_digest_uses_compact_markdown_links_and_readable_times():
    now = 1_800_000_000
    published = format_datetime(datetime.fromtimestamp(now - 300, timezone.utc))
    xml = ("<rss><channel><item><title>Clear headline</title>"
           "<link>https://publisher.example.com/articles/story?tracking=1</link>"
           f"<pubDate>{published}</pubDate><source>Example News</source>"
           "</item></channel></rss>")
    output = dated_news_digest(xml, now=now, limit=1)
    assert "1. [Clear headline](<https://publisher.example.com/articles/story?tracking=1>)" in output
    assert "— publisher.example.com · published 5m ago" in output
    assert "\n  https://" not in output


def test_stock_references_reuse_prior_symbols_and_requested_period():
    plan, question = compile_read(
        "How have these stocks trended over the past week?",
        last_user="Give me a stock update.", last_tools="get_stock_price",
        last_stock_response=_STOCK_RESPONSE,
    )
    assert not question
    assert plan == [("get_stock_price", {
        "symbols": ["GOOGL", "MU", "VICOR", "VOO"], "period": "past week",
    })]


def test_stock_context_requires_the_immediately_preceding_stock_exchange():
    assert adjacent_stock_response(_STOCK_RESPONSE, "get_stock_price") == _STOCK_RESPONSE
    assert adjacent_stock_response("Let's talk about the weather.", "get_weather") == ""
    result = compile_read(
        "all of them", last_user="How have these stocks trended over the past week?",
        last_stock_response=adjacent_stock_response("Let's talk about the weather.", "get_weather"),
    )
    assert result is None

    plan, question = compile_read(
        "all of them", last_user="How have these stocks trended over the past week?",
        last_stock_response=_STOCK_RESPONSE,
    )
    assert not question
    assert plan == [("get_stock_price", {
        "symbols": ["GOOGL", "MU", "VICOR", "VOO"], "period": "past week",
    })]


def test_closed_delivery_does_not_capture_unrelated_stock_reference():
    with tempfile.TemporaryDirectory() as temp:
        store = SessionStore(Path(temp) / "sessions.db")
        sid = store.create_session()
        first = prepare_turn(store, sid, "Send Mom my calendar tomorrow via Messages")
        assert first is not None
        assert finish_workflow(store, sid, first.plan, {"denied": True}) == "cancelled"
        assert prepare_turn(store, sid, "all of them") is None
