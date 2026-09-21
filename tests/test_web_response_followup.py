"""Regression coverage for the 2026-09-20 web-response debug sequence."""
from __future__ import annotations

import tempfile
from pathlib import Path

from service.memory.store import SessionStore
from service.workflows.engine import finish_workflow, prepare_turn
from service.workflows.reads import compile_read


_STOCK_RESPONSE = "GOOGL, MU, VICOR, and VOO each moved during today's session."


def test_current_news_reaches_ling_instead_of_structured_raw_read():
    assert compile_read("What is on the news today?") is None


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
