"""Stock continuations stay bound to the immediately preceding quote read."""
from __future__ import annotations

import asyncio
import json

import pytest

from service.memory.store import SessionStore
from service.tools.registry import REGISTRY, Tool
from service.workflows.reads import compile_read


async def _agent_events(main, session_id: str, prompt: str) -> list[dict]:
    response = await main.agent({
        "prompt": prompt,
        "session_id": session_id,
        "debug": False,
    })
    events = []
    async for item in response.body_iterator:
        if isinstance(item, bytes):
            item = item.decode()
        events.append(json.loads(item.removeprefix("data: ").strip()))
    return events


def _quoted_session(tmp_path, monkeypatch, symbols: list[str]):
    from service import main
    from service.memory import context

    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.create_session()
    store.add_turn(session_id, "user", " and ".join(symbols))
    store.add_turn(
        session_id,
        "assistant",
        "\n".join(f"{symbol}: 100 USD" for symbol in symbols),
        tool_digest="get_stock_price",
    )
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(context, "store", store)
    monkeypatch.setattr(main, "client", object(), raising=False)
    return main, store, session_id


@pytest.mark.parametrize("prompt", (
    "how are those stocks performing today",
    "what did these stocks do today",
    "how are our stocks doing today",
))
def test_plural_performance_continuations_keep_prior_symbols_and_period(
        tmp_path, monkeypatch, prompt):
    main, store, session_id = _quoted_session(
        tmp_path, monkeypatch, ["AAPL", "MSFT"])
    calls = []

    async def stock_read(**kwargs):
        calls.append(kwargs)
        return "Synthetic stock performance."

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("stock continuation escaped the structured read handler")

    monkeypatch.setitem(REGISTRY, "get_stock_price", Tool(
        "get_stock_price", "synthetic", {"properties": {
            "symbols": {"type": "array"}, "period": {"type": "string"}}},
        "web_read", stock_read))
    monkeypatch.setattr(main, "route", forbidden)
    monkeypatch.setattr(main, "ensure_omlx", forbidden)

    try:
        events = asyncio.run(_agent_events(main, session_id, prompt))
        assert calls == [{"symbols": ["AAPL", "MSFT"], "period": "today"}]
        call = next(event for event in events if event["type"] == "tool_call")
        assert call["name"] == "get_stock_price"
        assert call["args"] == calls[0]
        assert store.last_assistant_tools(session_id) == "get_stock_price"
    finally:
        store._db.close()


@pytest.mark.parametrize("prompt", (
    "how is this stock doing today",
    "how did that stock perform today",
))
def test_singular_performance_continuations_keep_one_prior_symbol(
        tmp_path, monkeypatch, prompt):
    main, store, session_id = _quoted_session(tmp_path, monkeypatch, ["AAPL"])
    calls = []

    async def stock_read(**kwargs):
        calls.append(kwargs)
        return "Synthetic stock performance."

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("stock continuation escaped the structured read handler")

    monkeypatch.setitem(REGISTRY, "get_stock_price", Tool(
        "get_stock_price", "synthetic", {"properties": {
            "symbols": {"type": "array"}, "period": {"type": "string"}}},
        "web_read", stock_read))
    monkeypatch.setattr(main, "route", forbidden)
    monkeypatch.setattr(main, "ensure_omlx", forbidden)

    try:
        asyncio.run(_agent_events(main, session_id, prompt))
        assert calls == [{"symbols": ["AAPL"], "period": "today"}]
    finally:
        store._db.close()


def test_stock_context_controls_do_not_guess_or_convert_other_intents():
    history = {
        "last_user": "AAPL and MSFT",
        "last_tools": "get_stock_price",
        "last_stock_response": "AAPL: 100 USD\nMSFT: 200 USD",
    }
    for prompt in (
        "how did the stock market perform today",
        "what is the forecast for these stocks tomorrow",
        "why are these stocks down today",
        "what are these stocks' P/E ratios today",
    ):
        assert compile_read(prompt, **history) is None

    planned, question = compile_read("how is this stock doing today", **history)
    assert planned == []
    assert question == "Which stock symbol or company name do you mean?"
