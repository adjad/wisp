"""Regression coverage for source-grounded current stock movements."""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from service.tools import web_tools


NY = ZoneInfo("America/New_York")


def _epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=NY).timestamp())


@pytest.fixture
def market_session_payloads() -> dict[str, dict]:
    previous = _epoch(2026, 9, 23, 9, 30)
    current = _epoch(2026, 9, 24, 9, 30)
    return {
        "intraday": {
            "meta": {
                "currency": "USD",
                "exchangeTimezoneName": "America/New_York",
                "regularMarketPrice": 105.0,
                "regularMarketTime": _epoch(2026, 9, 24, 14),
                "chartPreviousClose": 100.0,
                "marketState": "REGULAR",
                "currentTradingPeriod": {"regular": {
                    "start": current,
                    "end": _epoch(2026, 9, 24, 16),
                }},
            },
            "timestamp": [previous, current],
        },
        "weekend": {
            "meta": {
                "currency": "USD",
                "exchangeTimezoneName": "America/New_York",
                "regularMarketPrice": 102.0,
                "regularMarketTime": _epoch(2026, 9, 4, 16),
                "chartPreviousClose": 101.0,
                "marketState": "CLOSED",
            },
            "timestamp": [
                _epoch(2026, 9, 3, 9, 30),
                _epoch(2026, 9, 4, 9, 30),
            ],
        },
        "after_hours": {
            "meta": {
                "currency": "USD",
                "exchangeTimezoneName": "America/New_York",
                "regularMarketPrice": 100.0,
                "regularMarketTime": _epoch(2026, 9, 24, 16),
                "chartPreviousClose": 98.0,
                "postMarketPrice": 101.0,
                "postMarketTime": _epoch(2026, 9, 24, 17),
                "marketState": "POST",
            },
            "timestamp": [previous, current],
        },
    }


def test_intraday_quote_compares_with_previous_official_close(
        market_session_payloads):
    report = web_tools._quote_report(
        market_session_payloads["intraday"], "MU",
        now=_epoch(2026, 9, 24, 14, 5))

    assert "Quote: 105.00 USD" in report
    assert "2026-09-24 14:00 EDT" in report
    assert "intraday regular-session quote" in report
    assert "Previous official close: 100.00 USD on 2026-09-23" in report
    assert "Change from previous official close: +5.00 USD (+5.00%)" in report


def test_weekend_or_holiday_uses_last_sessions_not_calendar_days(
        market_session_payloads):
    report = web_tools._quote_report(
        market_session_payloads["weekend"], "VOO",
        now=_epoch(2026, 9, 7, 12))

    assert "latest official regular-session close" in report
    assert "source quote is 3 calendar days old" in report
    assert "Previous official close: 101.00 USD on 2026-09-03" in report
    assert "Change from previous official close: +1.00 USD (+0.99%)" in report


def test_after_hours_quote_compares_with_same_days_official_close(
        market_session_payloads):
    report = web_tools._quote_report(
        market_session_payloads["after_hours"], "MSFT",
        now=_epoch(2026, 9, 24, 17, 5))

    assert "after-hours quote" in report
    assert "Quote: 101.00 USD" in report
    assert "Previous official close: 100.00 USD on 2026-09-24" in report
    assert "Change from previous official close: +1.00 USD (+1.00%)" in report
    assert "98.00" not in report


def test_missing_prior_close_is_never_rendered_as_flat():
    payload = {
        "meta": {
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
            "regularMarketPrice": 50.0,
            "regularMarketTime": _epoch(2026, 9, 18, 16),
            "marketState": "CLOSED",
        },
        "timestamp": [_epoch(2026, 9, 18, 9, 30)],
    }
    report = web_tools._quote_report(
        payload, "GOOGL", now=_epoch(2026, 9, 24, 12))

    assert "source quote is 6 calendar days old" in report
    assert "Previous official close: unavailable from source" in report
    assert "Change from previous official close: unavailable" in report
    assert "do not infer unchanged or flat" in report
    assert "+0.00" not in report


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def test_alias_retry_reports_resolved_vicr_symbol(market_session_payloads):
    search = _Response(200, {"quotes": [{
        "symbol": "VICR",
        "quoteType": "EQUITY",
        "shortname": "Vicor Corporation",
    }]})
    quote = _Response(200, {"chart": {"result": [
        market_session_payloads["intraday"]]}})
    yahoo_get = AsyncMock(side_effect=[_Response(404), search, quote])

    with patch.object(web_tools, "_yahoo_get", yahoo_get), \
            patch.object(web_tools.time, "time", return_value=_epoch(2026, 9, 24, 14, 5)):
        report = asyncio.run(web_tools._one_quote("VICOR"))

    assert report.startswith("VICR (Vicor Corporation):")
    assert "VICOR:" not in report
    assert "Previous official close: 100.00 USD" in report
    assert yahoo_get.await_count == 3
