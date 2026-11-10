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


# --- which company a symbol means -----------------------------------------------
# A short company name written as a word ("Ford") was upper-cased into a ticker.
# When that was a real symbol of another company the chart call succeeded, so the
# wrong company's price came back under the name the user said, with no company
# shown. Words now go through search; a symbol taken as given is named from the
# chart. Company names and symbols below are synthetic.

class SearchResponse:
    def __init__(self, quotes, status=200):
        self.quotes, self.status_code = quotes, status

    def json(self):
        return {"quotes": self.quotes}


def _resolve(monkeypatch, query, search, **kwargs):
    urls = []

    async def fake_get(url, *args, **kw):
        urls.append(url)
        return search

    monkeypatch.setattr(web_tools, "_yahoo_get", fake_get)
    return asyncio.run(web_tools._resolve_symbol(query, **kwargs)), urls


MOTOR = SearchResponse([{"symbol": "SYNM", "quoteType": "EQUITY", "shortname": "Synth Motor Company"}])


@pytest.mark.parametrize("word", ["Synth", "synth", "sYNTH"])
def test_a_company_name_written_as_a_word_is_searched_not_upper_cased(monkeypatch, word):
    resolved, urls = _resolve(monkeypatch, word, MOTOR)
    assert resolved == ("SYNM", "Synth Motor Company")
    assert len(urls) == 1 and f"/v1/finance/search?q={word}" in urls[0]


def test_a_lower_case_ticker_still_resolves_through_search(monkeypatch):
    search = SearchResponse([{"symbol": "SYNT", "quoteType": "EQUITY", "shortname": "Synthetic Corp"}])
    resolved, urls = _resolve(monkeypatch, "synt", search)
    assert resolved == ("SYNT", "Synthetic Corp") and len(urls) == 1


@pytest.mark.parametrize("symbol", ["SYNTH", "S", "SYN.B", " SYNT "])
def test_a_symbol_already_in_capitals_skips_search(monkeypatch, symbol):
    resolved, urls = _resolve(monkeypatch, symbol, MOTOR)
    assert resolved == (symbol.strip(), symbol.strip()) and urls == []


@pytest.mark.parametrize("search", [
    "(timed out reaching Yahoo Finance)", SearchResponse([], status=429), SearchResponse([]),
], ids=["network", "http-429", "no-hit"])
def test_a_word_that_looks_like_a_ticker_falls_back_to_the_guess_when_search_cannot_answer(
        monkeypatch, search):
    resolved, urls = _resolve(monkeypatch, "Synth", search)
    assert resolved == ("SYNTH", "SYNTH") and len(urls) == 1


def test_the_retry_after_a_404_never_falls_back_to_the_guess_that_just_failed(monkeypatch):
    resolved, _ = _resolve(monkeypatch, "Synth", SearchResponse([], status=429), force_search=True)
    assert isinstance(resolved, str) and "429" in resolved


def test_a_longer_name_still_reports_a_search_failure(monkeypatch):
    resolved, _ = _resolve(monkeypatch, "Synthetic Motors", SearchResponse([]))
    assert isinstance(resolved, str) and "no ticker found" in resolved


def _quote_as_given(monkeypatch, meta, symbol="SYNTH"):
    """The real resolver on an all-caps symbol, then the chart."""
    async def fake_get(url, *args, **kwargs):
        assert "/v1/finance/search" not in url
        return Response(_one_day()) if "range=1d" in url else Response(_five_day(meta))

    monkeypatch.setattr(web_tools, "_yahoo_get", fake_get)
    return asyncio.run(web_tools._one_quote(symbol))


def test_a_symbol_taken_as_given_is_reported_with_the_company_from_the_chart(monkeypatch):
    report = _quote_as_given(monkeypatch, {"shortName": "Synth  Industries,\nInc.",
                                           "longName": "Synth Industries Incorporated"})
    assert report.startswith("SYNTH (Synth Industries, Inc.):")
    assert "Change from previous official close: +10.00 USD (+10.00%)" in report


def test_the_long_name_is_used_when_the_chart_has_no_short_name(monkeypatch):
    report = _quote_as_given(monkeypatch, {"shortName": " ", "longName": "Synth Industries Incorporated"})
    assert report.startswith("SYNTH (Synth Industries Incorporated):")


@pytest.mark.parametrize("meta", [{}, {"shortName": None}, {"shortName": 7}, {"shortName": "SYNTH"}],
                         ids=["absent", "null", "not-text", "same-as-symbol"])
def test_a_chart_without_a_usable_name_keeps_the_bare_symbol(monkeypatch, meta):
    assert _quote_as_given(monkeypatch, meta).startswith("SYNTH:")


def test_a_chart_name_cannot_make_a_good_quote_look_failed(monkeypatch):
    async def fake_get(url, *args, **kwargs):
        return Response(_one_day()) if "range=1d" in url else Response(
            _five_day({"shortName": "Synth: (Holdings)"}))

    monkeypatch.setattr(web_tools, "_yahoo_get", fake_get)
    report = asyncio.run(web_tools.get_stock_price(["SYNTH"]))
    assert report.startswith("SYNTH (Synth (Holdings)):") and "incomplete stock lookup" not in report


def test_a_searched_name_wins_over_the_chart_name(monkeypatch):
    report, _ = _quote(monkeypatch, _five_day({"shortName": "Another Name"}), Response(_one_day()), "SYNT")
    assert report.startswith("SYNT (Synthetic Index):")


def test_history_names_the_company_for_a_symbol_taken_as_given(monkeypatch):
    payload = _five_day({"shortName": "Synth Industries, Inc."})

    async def fake_get(url, *args, **kwargs):
        assert "/v1/finance/search" not in url and "range=5d" in url
        return Response(payload)

    monkeypatch.setattr(web_tools, "_yahoo_get", fake_get)
    report = asyncio.run(web_tools._one_history("SYNTH", "5d"))
    assert report.startswith("SYNTH (Synth Industries, Inc.) over 5d (")
