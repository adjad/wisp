"""Offline weather location checks; fake HTTP never reads a live provider."""
import asyncio
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest

from service.tools import web_tools
from service.tools.ca_locations import (
    CA_CITIES, normalize_weather_location, provider_area_matches,
)


def area(city="San Francisco", region="California", country="United States"):
    return {key: [{"value": value}] for key, value in (
        ("areaName", city), ("region", region), ("country", country),
    )}


def payload(city="San Francisco", region="California", country="United States"):
    return {
        "nearest_area": [area(city, region, country)],
        "current_condition": [{"temp_F": "99", "temp_C": "37",
            "weatherDesc": [{"value": "Sunny"}], "FeelsLikeF": "98",
            "humidity": "20", "windspeedMiles": "5"}],
        "weather": [
            {"date": "2026-10-12", "maxtempF": "80", "mintempF": "60",
             "hourly": [{"weatherDesc": [{"value": "Sunny"}], "chanceofrain": "0"}]},
            {"date": "2026-10-13", "maxtempF": "70", "mintempF": "50",
             "hourly": [{"weatherDesc": [{"value": "Cloudy"}], "chanceofrain": "20"}]},
        ],
    }


@pytest.fixture(autouse=True)
def deny_live_weather(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("weather test attempted an unmocked HTTP request")
    monkeypatch.setattr(web_tools, "_yahoo_get", denied)


def fake_weather(monkeypatch, data, *, status=200):
    calls = []
    async def get(url):
        calls.append(url)
        return SimpleNamespace(status_code=status, json=lambda: deepcopy(data))
    monkeypatch.setattr(web_tools, "_yahoo_get", get)
    return calls


@pytest.mark.parametrize("city", CA_CITIES)
def test_all_twenty_canonical_cities_normalize(city):
    for query in (f"{city},CA", f"{city.lower()}, California", f"{city} CA",
                  f"{city}, CA, USA"):
        result = normalize_weather_location(query)
        assert result.query == f"{city},CA"
        assert result.california_city == city


@pytest.mark.parametrize("query,city", [
    ("SF", "San Francisco"), ("S.F.", "San Francisco"),
    ("SF, CA", "San Francisco"), ("LA, CA", "Los Angeles"),
    ("SD, California", "San Diego"), ("SJ, CA", "San Jose"),
    ("Santa Clra, CA", "Santa Clara"), ("Santa Crz, California", "Santa Cruz"),
    ("San Fransisco, CA", "San Francisco"), ("San Jsoe, CA", "San Jose"),
    ("San José, California", "San Jose"), ("  San   Jose , CA  ", "San Jose"),
    ("SF, CA.", "San Francisco"), ("SF, C.A.", "San Francisco"),
    ("San Jose, Calif.", "San Jose"),
])
def test_finite_aliases_and_fixture_typos(query, city):
    result = normalize_weather_location(query)
    assert result.query == f"{city},CA" and result.california_city == city


@pytest.mark.parametrize("query", [
    "Santa, California", "Santa C, CA", "SC, CA", "San, CA", "CA", "California",
    "LA", "SD", "SJ", "Santa Clara", "San Francisco", "San Jsoe",
    "San Jose, CA, Canada", "San Francisco, California, Argentina", "Santa/Clara, CA",
    "San Francisco (CA)", "SF, CA!", "SF, C.A., Canada", "SF, Calif, Argentina",
    "CA,CA", "California,CA",
])
def test_ambiguous_or_conflicting_locations_refuse_before_http(query):
    result = asyncio.run(web_tools.get_weather(query))
    assert "ask" in result and "guess" in result


@pytest.mark.parametrize("query", ["", " ", "tomorrow", "next month"])
def test_missing_or_temporal_location_refuses_before_http(query):
    assert "ask the user" in asyncio.run(web_tools.get_weather(query))


@pytest.mark.parametrize("city", CA_CITIES)
def test_weather_requires_and_returns_the_requested_california_city(city, monkeypatch):
    calls = fake_weather(monkeypatch, payload(city))
    result = asyncio.run(web_tools.get_weather(f"{city}, California"))
    assert f"Weather for {city}, California, United States" in result
    assert "99°F" in result
    assert unquote(urlparse(calls[0]).path) == f"/{city},CA"
    assert len(calls) == 1


@pytest.mark.parametrize("query,city", [
    ("SF", "San Francisco"), ("Santa Clra,CA", "Santa Clara"),
    ("Santa Crz,California", "Santa Cruz"), ("San Fransisco,CA", "San Francisco"),
    ("San Jsoe,CA", "San Jose"), ("Dublin,CA", "Dublin"),
])
def test_normalized_queries_verify_the_canonical_city(query, city, monkeypatch):
    calls = fake_weather(monkeypatch, payload(city))
    result = asyncio.run(web_tools.get_weather(query))
    assert f"Weather for {city}, California, United States" in result
    assert unquote(urlparse(calls[0]).path) == f"/{city},CA"


@pytest.mark.parametrize("city", CA_CITIES)
@pytest.mark.parametrize("mismatch", ["city", "region", "country"])
def test_wrong_provider_city_state_or_country_never_returns_numbers(city, mismatch, monkeypatch):
    values = {"city": city, "region": "California", "country": "United States"}
    values[mismatch] = {"city": "Other City", "region": "Cordoba", "country": "Argentina"}[mismatch]
    fake_weather(monkeypatch, payload(**values))
    result = asyncio.run(web_tools.get_weather(f"{city},CA"))
    assert "could not verify" in result
    assert "99" not in result and "80" not in result and "°" not in result


@pytest.mark.parametrize("bad_area", [
    {}, {"areaName": [{"value": "San Francisco"}]},
    area(country=""), area(region=""), area(city=""),
    {"areaName": [{"value": 10}], **{k: v for k, v in area().items() if k != "areaName"}},
])
def test_missing_or_malformed_provider_identity_is_not_weather_evidence(bad_area, monkeypatch):
    data = payload()
    data["nearest_area"] = [bad_area]
    fake_weather(monkeypatch, data)
    result = asyncio.run(web_tools.get_weather("San Francisco,CA"))
    assert "could not verify" in result and "99" not in result


def test_multiple_provider_areas_are_ambiguous(monkeypatch):
    data = payload()
    data["nearest_area"].append(area())
    fake_weather(monkeypatch, data)
    assert "could not verify" in asyncio.run(web_tools.get_weather("SF"))


@pytest.mark.parametrize("country", ["United States", "United States of America", "US", "U.S.A."])
def test_supported_provider_identity_spellings(country):
    assert provider_area_matches(area("San José", "CA", country), "San Jose")


@pytest.mark.parametrize("query,city,country", [
    ("London", "London", "United Kingdom"),
    ("San Jose, Costa Rica", "San Jose", "Costa Rica"),
    ("Paris, France", "Paris", "France"),
    ("SFO", "San Francisco International Airport", "United States"),
    ("94568", "Dublin", "United States"),
])
def test_other_places_postcodes_and_airports_keep_the_existing_lookup(query, city, country, monkeypatch):
    calls = fake_weather(monkeypatch, payload(city, "Other Region", country))
    result = asyncio.run(web_tools.get_weather(query))
    assert f"Weather for {city}, Other Region, {country}" in result
    assert unquote(urlparse(calls[0]).path) == f"/{query}"


def test_tomorrow_uses_only_requested_forecast_and_requires_coverage(monkeypatch):
    from service.tools import timeranges
    real_resolve = timeranges.resolve_span
    monkeypatch.setattr(timeranges, "resolve_span", lambda p: real_resolve(p, now=datetime(2026, 10, 12, 9)))
    fake_weather(monkeypatch, payload())
    result = asyncio.run(web_tools.get_weather("SF", period="tomorrow"))
    assert "2026-10-13" in result and "2026-10-12" not in result and "now:" not in result
    assert "70°F" in result and "99°F" not in result
    assert "does not cover" in asyncio.run(web_tools.get_weather("SF", period="this week"))
    assert "unsupported weather range" in asyncio.run(web_tools.get_weather("SF", period="nonsense range"))


@pytest.mark.parametrize("data", [{}, {"current_condition": [], "nearest_area": []},
                                       {"current_condition": ["wrong"], "nearest_area": [area()]},
                                       {**payload(), "weather": "wrong"},
                                       {**payload(), "weather": ["wrong"]}])
def test_malformed_http_payload_fails_without_numbers(data, monkeypatch):
    fake_weather(monkeypatch, data)
    result = asyncio.run(web_tools.get_weather("SF"))
    assert "unexpected shape" in result and "99" not in result


def test_http_error_and_fetch_failure_remain_honest(monkeypatch):
    calls = fake_weather(monkeypatch, payload(), status=503)
    result = asyncio.run(web_tools.get_weather("SF"))
    assert "HTTP 503" in result and "99" not in result and len(calls) == 1
    async def failed(url):
        return "(timed out reaching Yahoo Finance)"
    monkeypatch.setattr(web_tools, "_yahoo_get", failed)
    assert "weather service" in asyncio.run(web_tools.get_weather("SF"))


def dispatch_weather(**args):
    from service.tools.registry import get_tool, run_tool
    return asyncio.run(run_tool(get_tool("get_weather"), args))


@pytest.mark.parametrize("period", ["tonight", "tomorrow morning", "this evening",
    "today afternoon", "next weekend", "nonsense range"])
def test_unsupported_time_constraints_fail_before_http(period, monkeypatch):
    calls = fake_weather(monkeypatch, payload())
    result = dispatch_weather(location="SF", period=period)
    assert "unsupported weather range" in result and not calls


@pytest.mark.parametrize("location", ["Tokyo tomorrow morning", "Tokyo tonight",
    "London this evening", "Paris next weekend", "Tokyo morning",
    "Tokyo on 2026-10-12", "Tokyo at 6pm", "Tokyo 6p.m.", "Tokyo 18:00",
    "Tokyo next Friday", "Tokyo Friday", "Tokyo morning please", "Tokyo night",
    "Tokyo midday", "Tokyo right now", "Tokyo overnight", "Tokyo weekend"])
def test_temporal_suffix_is_not_a_location(location, monkeypatch):
    calls = fake_weather(monkeypatch, payload("Tokyo", "Tokyo", "Japan"))
    result = dispatch_weather(location=location)
    assert "time" in result and not calls and "99°F" not in result


@pytest.mark.parametrize("target", ["temp_F", "temp_C", "FeelsLikeF", "humidity", "windspeedMiles",
                                     "maxtempF", "mintempF", "chanceofrain"])
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "garbage", "2_0", True, {}, None])
def test_provider_numbers_are_validated_before_any_weather_output(target, bad, monkeypatch):
    data = payload()
    node = (data["current_condition"][0] if target in data["current_condition"][0]
            else data["weather"][0]["hourly"][0] if target == "chanceofrain"
            else data["weather"][0])
    node[target] = bad
    fake_weather(monkeypatch, data)
    result = dispatch_weather(location="SF")
    from service.tools.registry import classify_tool_outcome
    assert classify_tool_outcome("get_weather", result).status == "failed", result
    assert "°F" not in result and "chance of rain" not in result


@pytest.mark.parametrize("target,bad", [("humidity", -1), ("humidity", 101),
    ("chanceofrain", -1), ("chanceofrain", 101), ("windspeedMiles", -1)])
def test_provider_percentages_and_wind_have_valid_ranges(target, bad, monkeypatch):
    data = payload()
    node = data["weather"][0]["hourly"][0] if target == "chanceofrain" else data["current_condition"][0]
    node[target] = bad
    fake_weather(monkeypatch, data)
    assert dispatch_weather(location="SF").startswith("(error:")


@pytest.mark.parametrize("date", [None, 20261012, {}, "2026-02-30", "2026-1-2", "20261012", "NaN"])
def test_provider_dates_are_canonical_calendar_dates(date, monkeypatch):
    data = payload()
    data["weather"][0]["date"] = date
    fake_weather(monkeypatch, data)
    assert dispatch_weather(location="SF").startswith("(error:")


@pytest.mark.parametrize("kind", ["duplicate", "reversed", "gap"])
def test_provider_forecast_date_sequence_is_unambiguous(kind, monkeypatch):
    data = payload()
    if kind == "duplicate": data["weather"][1]["date"] = data["weather"][0]["date"]
    elif kind == "reversed": data["weather"].reverse()
    else: data["weather"][1]["date"] = "2026-10-14"
    fake_weather(monkeypatch, data)
    assert dispatch_weather(location="SF").startswith("(error:")


def test_missing_rain_is_unknown_and_forecast_uses_absolute_dates(monkeypatch):
    data = payload()
    del data["weather"][0]["hourly"][0]["chanceofrain"]
    fake_weather(monkeypatch, data)
    result = dispatch_weather(location="SF")
    assert "chance of rain unknown" in result and ", 0% chance of rain" not in result
    assert "forecast (2026-10-12)" in result and "forecast (2026-10-13)" in result
    assert "today (" not in result and "tomorrow (" not in result


@pytest.mark.parametrize("kind", ["empty", "too_many", "bad_hours", "bad_hour", "bad_high_low"])
def test_invalid_forecast_carriers_never_leak_current_numbers(kind, monkeypatch):
    data = payload()
    if kind == "empty": data["weather"] = []
    elif kind == "too_many": data["weather"] *= 2
    elif kind == "bad_hours": data["weather"][0]["hourly"] = "wrong"
    elif kind == "bad_hour": data["weather"][0]["hourly"] = ["wrong"]
    else: data["weather"][0]["mintempF"] = "81"
    fake_weather(monkeypatch, data)
    result = dispatch_weather(location="SF")
    assert result.startswith("(error:") and "99°F" not in result


def test_valid_signed_decimal_and_explicit_zero_measurements_are_preserved(monkeypatch):
    data = payload()
    data["current_condition"][0].update(temp_F=-4, temp_C="-20.0", humidity=0, windspeedMiles=0.5)
    fake_weather(monkeypatch, data)
    result = dispatch_weather(location="SF")
    from service.tools.registry import classify_tool_outcome
    assert classify_tool_outcome("get_weather", result).status == "succeeded", result
    assert "-4°F (-20.0°C)" in result and "humidity 0%" in result and "wind 0.5 mph" in result
    assert ", 0% chance of rain" in result


@pytest.mark.parametrize("kind", ["http", "shape", "identity", "fetch"])
def test_legacy_failure_controls_are_registry_failures(kind, monkeypatch):
    data = payload(city="Wrong" if kind == "identity" else "San Francisco")
    if kind == "shape": data = {}
    fake_weather(monkeypatch, data, status=503 if kind == "http" else 200)
    if kind == "fetch":
        async def failed(url): return "(timed out reaching Yahoo Finance)"
        monkeypatch.setattr(web_tools, "_yahoo_get", failed)
    from service.tools.registry import classify_tool_outcome
    assert classify_tool_outcome("get_weather", dispatch_weather(location="SF")).status == "failed"


def test_shifted_provider_start_date_is_never_labeled_today(monkeypatch):
    data = payload()
    data["weather"] = [data["weather"][1]]
    fake_weather(monkeypatch, data)
    result = dispatch_weather(location="SF")
    assert "forecast (2026-10-13)" in result and "today (" not in result


@pytest.mark.parametrize("period,day", [("today", "2026-10-12"), ("tomorrow", "2026-10-13"),
                                        ("2026-10-13", "2026-10-13")])
def test_requested_dates_are_preserved_through_registry_dispatch(period, day, monkeypatch):
    from service.tools import timeranges
    real_resolve = timeranges.resolve_span
    monkeypatch.setattr(timeranges, "resolve_span", lambda p: real_resolve(p, now=datetime(2026, 10, 12, 9)))
    calls = fake_weather(monkeypatch, payload())
    result = dispatch_weather(location="SF", period=period)
    assert f"forecast ({day})" in result and "now:" not in result and len(calls) == 1


def test_missing_requested_date_never_returns_unrequested_numbers(monkeypatch):
    calls = fake_weather(monkeypatch, payload())
    result = dispatch_weather(location="SF", period="2026-10-14")
    assert result.startswith("(error:") and "does not cover" in result
    assert "99°F" not in result and "80°F" not in result and len(calls) == 1


@pytest.mark.parametrize("period", [
    "2026-10-12T18:00:00", "2026-10-12 18:00", "20261012T180000",
    "2026-10-12T18:00:00-07:00", "2026-10-12T18:00:00Z",
    "2026-10-12T00:00:00", "2026-10-12X18:00:00",
    "2026-W42-1T18:00:00", "2026-10-12+05:00",
    "2026-10-12T18:00:00.123456",
])
def test_iso_datetime_scope_is_rejected_before_http(period, monkeypatch):
    calls = fake_weather(monkeypatch, payload())
    result = dispatch_weather(location="SF", period=period)
    from service.tools.registry import classify_tool_outcome
    assert classify_tool_outcome("get_weather", result).status == "failed", result
    assert "unsupported weather range" in result and calls == []
    assert "°F" not in result and "chance of rain" not in result


@pytest.mark.parametrize("separator", [" and ", " to ", " through ", " thru ",
                                        " until ", " -- ", " : ", ".."])
@pytest.mark.parametrize("clock_first", [False, True])
def test_compound_datetime_scope_is_rejected_before_http(separator, clock_first, monkeypatch):
    date, clock = "2026-10-13", "2026-10-12T18:00:00-07:00"
    period = separator.join((clock, date) if clock_first else (date, clock))
    calls = fake_weather(monkeypatch, payload())
    result = dispatch_weather(location="SF", period=period)
    from service.tools.registry import classify_tool_outcome
    assert classify_tool_outcome("get_weather", result).status == "failed", result
    assert "unsupported weather range" in result and calls == []
    assert "°F" not in result and "chance of rain" not in result


@pytest.mark.parametrize("period,dates", [
    ("today", {"2026-10-12"}), ("tomorrow", {"2026-10-13"}),
    ("2026-10-12", {"2026-10-12"}), ("20261012", {"2026-10-12"}),
    ("today and tomorrow", {"2026-10-12", "2026-10-13"}),
])
def test_datetime_guard_preserves_existing_date_only_periods(period, dates, monkeypatch):
    from service.tools import timeranges
    real_resolve = timeranges.resolve_span
    monkeypatch.setattr(timeranges, "resolve_span", lambda p: real_resolve(p, now=datetime(2026, 10, 12, 9)))
    calls = fake_weather(monkeypatch, payload())
    result = dispatch_weather(location="SF", period=period)
    from service.tools.registry import classify_tool_outcome
    assert classify_tool_outcome("get_weather", result).status == "succeeded", result
    assert calls and len(calls) == 1 and "now:" not in result
    assert {day for day in ("2026-10-12", "2026-10-13")
            if f"forecast ({day})" in result} == dates


@pytest.mark.parametrize("separator", [" and ", " to ", " through ", " thru ",
                                        " until ", " -- ", " : ", ".."])
def test_datetime_guard_preserves_existing_date_only_ranges(separator, monkeypatch):
    period = separator.join(("2026-10-12", "2026-10-13"))
    calls = fake_weather(monkeypatch, payload())
    result = dispatch_weather(location="SF", period=period)
    from service.tools.registry import classify_tool_outcome
    assert classify_tool_outcome("get_weather", result).status == "succeeded", result
    assert len(calls) == 1 and "now:" not in result
    assert "forecast (2026-10-12)" in result and "forecast (2026-10-13)" in result


@pytest.mark.parametrize("location", [
    "Tokyo 20261012T180000", "Tokyo 2026-W42-1T180000", "Tokyo at 1800",
    "Tokyo 20261012t180000", "Tokyo 2026W421T180000",
    "Tokyo 20261012X180000", "Tokyo 2026-W42-1X180000",
    "Tokyo 20261012T180000+0900", "Tokyo 2026-W42-1T180000+09:00",
    "Tokyo 20261012", "Tokyo 2026-W42-1", "Tokyo 2026W421",
    "Tokyo 20269999T180000", "Tokyo 2026-W99-9T180000",
    "Tokyo at 0000", "Tokyo at 2400", "Tokyo at 180000", "Tokyo at 999999",
])
def test_compact_temporal_location_constraint_is_rejected_before_fetch(location, monkeypatch):
    calls = fake_weather(monkeypatch, payload("Tokyo", "Tokyo", "Japan"))
    result = dispatch_weather(location=location)
    from service.tools.registry import classify_tool_outcome
    assert classify_tool_outcome("get_weather", result).status == "failed", result
    assert "time constraint" in result and calls == []
    assert "°F" not in result and "chance of rain" not in result


@pytest.mark.parametrize("location,city,region,country", [
    ("Tokyo", "Tokyo", "Tokyo", "Japan"),
    ("Tokyo, Japan", "Tokyo", "Tokyo", "Japan"),
    ("94568", "Dublin", "California", "United States"),
    ("01310930", "São Paulo", "São Paulo", "Brazil"),
    ("DUB", "Dublin", "Leinster", "Ireland"),
    ("Dublin, Ireland", "Dublin", "Leinster", "Ireland"),
    ("San José, Costa Rica", "San José", "San José", "Costa Rica"),
    ("SF", "San Francisco", "California", "United States"),
])
@pytest.mark.parametrize("period", ["", "2026-10-12", "20261012",
                                     "2026-10-12 through 2026-10-13"])
def test_compact_location_guard_preserves_city_and_date_arguments(location, city, region, country, period, monkeypatch):
    calls = fake_weather(monkeypatch, payload(city, region, country))
    result = dispatch_weather(location=location, period=period)
    from service.tools.registry import classify_tool_outcome
    assert classify_tool_outcome("get_weather", result).status == "succeeded", result
    assert len(calls) == 1 and f"Weather for {city}" in result
    assert "forecast (2026-10-12)" in result
