"""Weather place resolution: filler is refused, big cities beat tiny namesakes."""
import asyncio

from service.tools import web_tools


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


def _install(monkeypatch, results):
    calls = []

    async def fake(url):
        calls.append(url)
        if "geocoding-api" in url:
            return _Resp({"results": results})
        return _Resp({
            "current_condition": [{"temp_F": "50", "temp_C": "10", "FeelsLikeF": "48",
                                   "humidity": "50", "windspeedMiles": "5",
                                   "weatherDesc": [{"value": "Clear"}]}],
            "nearest_area": [{"areaName": [{"value": "Somewhere"}]}],
            "weather": [{"date": "2026-11-14", "maxtempF": "60", "mintempF": "40",
                         "hourly": [{"weatherDesc": [{"value": "Sunny"}], "chanceofrain": "0"}] * 5}]})
    monkeypatch.setattr(web_tools, "_yahoo_get", fake)
    return calls


def test_filler_location_is_never_looked_up(monkeypatch):
    calls = _install(monkeypatch, [])
    for q in ("home city,ST", "weather", "today", "here", "my location", "current location"):
        out = asyncio.run(web_tools.get_weather(q))
        assert out.startswith("(error: no location given"), (q, out)
    assert calls == []


def test_chicago_picks_most_populous_exact_match(monkeypatch):
    calls = _install(monkeypatch, [
        {"name": "Mccormickville", "admin1": "Illinois", "country": "United States",
         "latitude": 1.0, "longitude": 1.0, "population": 10},
        {"name": "Chicago Heights", "admin1": "Illinois", "country": "United States",
         "latitude": 2.0, "longitude": 2.0, "population": 30000},
        {"name": "Chicago", "admin1": "Illinois", "country": "United States",
         "latitude": 41.85, "longitude": -87.65, "population": 2700000},
        {"name": "Chicago", "admin1": "Indiana", "country": "United States",
         "latitude": 3.0, "longitude": 3.0, "population": 500},
    ])
    for q in ("Chicago,IL", "Chicago weather, IL", "Chicago, Illinois"):
        calls.clear()
        out = asyncio.run(web_tools.get_weather(q))
        assert "Chicago, Illinois" in out, out
        assert any("41.85%2C-87.65" in u for u in calls), calls
        assert not any("wttr.in/Chicago" in u for u in calls)
