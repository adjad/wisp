"""Stock continuations stay bound to the immediately preceding quote read."""
from __future__ import annotations

import asyncio
import json

import pytest

from service.memory.store import SessionStore
from service.tools.registry import REGISTRY, Tool
from service.workflows import reads
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


def _quoted_session(tmp_path, monkeypatch, symbols: list[str], *, last_user: str = ""):
    from service import main
    from service.memory import context

    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.create_session()
    store.add_turn(session_id, "user", last_user or " and ".join(symbols))
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


@pytest.mark.parametrize("prompt", (
    "show prices of Apple shares but not Microsoft shares",
    "show quotes for Apple shares excluding Microsoft shares",
    "show prices of these shares except Microsoft shares",
    "show prices of Apple shares, not Microsoft shares",
    "show prices of these shares, not Microsoft shares",
    "show prices of Apple shares not Microsoft shares",
    "show prices of Apple shares without Microsoft shares",
    "show prices of these shares other than MSFT",
))
def test_excluded_shares_are_never_fetched(tmp_path, monkeypatch, prompt):
    main, store, session_id = _quoted_session(
        tmp_path, monkeypatch, ["AAPL", "MSFT"], last_user="AAPL and MSFT last week")
    calls = []

    async def stock_read(**kwargs):
        calls.append(kwargs)
        return "Synthetic stock quote."

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("stock exclusion escaped the structured read handler")

    monkeypatch.setitem(REGISTRY, "get_stock_price", Tool(
        "get_stock_price", "synthetic", {"properties": {
            "symbols": {"type": "array"}, "period": {"type": "string"}}},
        "web_read", stock_read))
    monkeypatch.setattr(main, "route", forbidden)
    monkeypatch.setattr(main, "ensure_omlx", forbidden)

    try:
        asyncio.run(_agent_events(main, session_id, prompt))
        assert calls == [{"symbols": ["AAPL"], "period": "last week"}]
    finally:
        store._db.close()


@pytest.mark.parametrize(("prompt", "expected_symbols", "expected_period"), (
    ("show the latest price of these shares", ["AAPL", "MSFT"], None),
    ("show current prices of Apple shares", ["AAPL"], None),
    ("show prices of these shares", ["AAPL", "MSFT"], "last week"),
))
def test_fresh_quotes_do_not_inherit_historical_period(
        tmp_path, monkeypatch, prompt, expected_symbols, expected_period):
    main, store, session_id = _quoted_session(
        tmp_path, monkeypatch, ["AAPL", "MSFT"], last_user="AAPL and MSFT last week")
    calls = []

    async def stock_read(**kwargs):
        calls.append(kwargs)
        return "Synthetic stock quote."

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
        expected = {"symbols": expected_symbols}
        if expected_period:
            expected["period"] = expected_period
        assert calls == [expected]
    finally:
        store._db.close()


@pytest.mark.parametrize("prompt", (
    "show prices of all but Apple shares",
    "what are the prices of Apple and not Microsoft shares",
))
def test_ambiguous_stock_exclusion_clarifies_instead_of_fetching(tmp_path, monkeypatch, prompt):
    history = {
        "last_user": "AAPL and MSFT last week",
        "last_tools": "get_stock_price",
        "last_stock_response": "AAPL: 100 USD\nMSFT: 200 USD",
    }
    assert compile_read(prompt, **history) == (
        [], "Which stock symbols or company names should I include?")
    main, store, session_id = _quoted_session(
        tmp_path, monkeypatch, ["AAPL", "MSFT"], last_user=history["last_user"])
    calls = []

    async def stock_read(**kwargs):
        calls.append(kwargs)
        return "Synthetic stock quote."

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("ambiguous stock exclusion escaped the structured read handler")

    monkeypatch.setitem(REGISTRY, "get_stock_price", Tool(
        "get_stock_price", "synthetic", {"properties": {
            "symbols": {"type": "array"}, "period": {"type": "string"}}},
        "web_read", stock_read))
    monkeypatch.setattr(main, "route", forbidden)
    monkeypatch.setattr(main, "ensure_omlx", forbidden)

    try:
        asyncio.run(_agent_events(main, session_id, prompt))
        assert calls == []
    finally:
        store._db.close()


def test_unparsed_stock_exclusion_with_another_action_defers():
    assert compile_read(
        "show prices of Apple shares but not Microsoft shares and email Sam",
        last_user="AAPL and MSFT last week",
        last_tools="get_stock_price",
        last_stock_response="AAPL: 100 USD\nMSFT: 200 USD",
    ) is None


@pytest.mark.parametrize(("prompt", "expected_symbol"), (
    ("show stock prices of Palantir shares, not PLTR shares", None),
    ("show stock prices of Rivian shares, not RIVN shares", None),
    ("show stock prices of PLTR shares, not Palantir shares", None),
    ("show stock prices of Apple shares, not PLTR shares", "aapl"),
    ("show stock prices of Apple shares, not Palantir shares", "aapl"),
))
def test_structured_stock_alias_exclusions_guard_actual_read(
        monkeypatch, prompt, expected_symbol):
    calls = []

    async def fake_run(tool, args):
        calls.append((tool.name, args))
        return "Synthetic quote."

    async def emit(_event):
        pass

    monkeypatch.setattr(reads, "run_tool", fake_run)
    compiled = compile_read(prompt)
    asyncio.run(reads.execute_read(compiled, emit))
    if expected_symbol is None:
        assert compiled == ([], "Which stock symbols or company names should I include?")
        assert calls == []
    else:
        assert len(calls) == 1
        assert calls[0][0] == "get_stock_price"
        assert [reads.stock_symbol_key(symbol) for symbol in calls[0][1]["symbols"]] == [
            expected_symbol]


def test_stock_exclusion_guard_keeps_unrelated_email_read():
    assert compile_read("show email summaries but not messages") == ([("summarize_emails", {})], "")
