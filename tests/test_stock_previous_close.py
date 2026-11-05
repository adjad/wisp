"""A quote reports how far it moved when two source responses agree on the prior close.

Yahoo's 5-day chart stopped sending previousClose, and a daily bar's stamp is its
session open, so every symbol (index or not) reported its change as unavailable. The
1-day chart's chartPreviousClose fills that in only when it corroborates the 5-day
chart's prior bar. Everything else keeps the honest wording. All numbers are synthetic.

    .venv/bin/python -m pytest tests/test_stock_previous_close.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-stock-close-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from service.tools import web_tools  # noqa: E402


def _at(day, hour, minute=0):
    return int(datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc).timestamp())


NEW_YORK = "America/New_York"
OPEN_DAY_1, OPEN_DAY_2, OPEN_DAY_3 = _at(22, 13, 30), _at(23, 13, 30), _at(24, 13, 30)
SESSION = {"regular": {"start": _at(24, 13, 30), "end": _at(24, 20)}}
UNAVAILABLE = "Previous official close: unavailable from source"


def _five_day(meta=None, stamps=(OPEN_DAY_1, OPEN_DAY_2, OPEN_DAY_3), closes=(90.0, 100.0000001, 110.0)):
    return {"meta": {"regularMarketPrice": 110.0, "regularMarketTime": _at(24, 20, 51),
                     "exchangeTimezoneName": NEW_YORK, "currency": "USD", "chartPreviousClose": 80.0,
                     "currentTradingPeriod": SESSION, **(meta or {})},
            "timestamp": list(stamps), "indicators": {"quote": [{"close": list(closes)}]}}


def _one_day(close=100.0, stamp=_at(24, 20, 51)):
    return {"meta": {"chartPreviousClose": close, "regularMarketTime": stamp, "regularMarketPrice": 110.0},
            "timestamp": [stamp], "indicators": {"quote": [{"close": [110.0]}]}}


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return {"chart": {"result": [self.payload]}}


def _quote(monkeypatch, five, one, symbol="^SYN"):
    urls = []

    async def fake_get(url, *args, **kwargs):
        urls.append(url)
        return one if "range=1d" in url else Response(five)

    async def fake_resolve(query):
        return symbol, "Synthetic Index"

    monkeypatch.setattr(web_tools, "_yahoo_get", fake_get)
    monkeypatch.setattr(web_tools, "_resolve_symbol", fake_resolve)
    return asyncio.run(web_tools._one_quote(symbol)), urls


@pytest.mark.parametrize("symbol", ["^SYN", "SYNT"])
def test_an_agreeing_prior_close_gives_the_real_change(monkeypatch, symbol):
    report, urls = _quote(monkeypatch, _five_day(), Response(_one_day()), symbol)
    assert "Previous official close: 100.00 USD on 2026-09-23" in report
    assert "Change from previous official close: +10.00 USD (+10.00%)" in report
    assert len(urls) == 2


@pytest.mark.parametrize("one", [
    Response(_one_day(close=99.0)),                     # the two responses disagree
    Response(_one_day(close=0)),                        # nonpositive
    Response(_one_day(close=None)),                     # missing
    Response(_one_day(stamp=_at(23, 20))),              # a different session
    Response(_one_day(), status=500),
    Response(ValueError("not json")),
    "(network error)",
], ids=["disagree", "zero", "missing", "other-session", "http-500", "bad-json", "network"])
def test_anything_short_of_agreement_stays_unavailable(monkeypatch, one):
    report, _ = _quote(monkeypatch, _five_day(), one)
    assert "Quote: 110.00 USD" in report
    assert UNAVAILABLE in report
    assert "do not infer unchanged or flat" in report
    assert "100.00" not in report and "80.00" not in report


@pytest.mark.parametrize("five", [
    _five_day(stamps=(OPEN_DAY_3,), closes=(110.0,)),            # no prior bar
    _five_day(closes=(90.0, None, 110.0)),                       # a null prior bar
], ids=["no-prior-bar", "null-prior-bar"])
def test_nothing_to_corroborate_means_no_second_request(monkeypatch, five):
    report, urls = _quote(monkeypatch, five, Response(_one_day()))
    assert len(urls) == 1
    assert UNAVAILABLE in report


def test_a_source_previous_close_needs_no_second_request(monkeypatch):
    report, urls = _quote(monkeypatch, _five_day({"previousClose": 100.0}), Response(_one_day(close=1.0)))
    assert len(urls) == 1
    assert "Previous official close: 100.00 USD on 2026-09-23" in report
