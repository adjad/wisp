"""rc1 answer polish: no blank lines around an answer, and weather as a sentence.

Live run of rc1 on Gemma 4 E4B: narrated answers began with "\\n", "\\n\\n\\n",
and "What's the weather like in San Francisco today?" showed get_weather's
raw report as the reply. Both are fixed on the backend; only formatting moved,
never the model's wording.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-answer-polish-"))

from service.answer_text import AnswerEdgeTrimmer  # noqa: E402
from service.workflows.weather_reply import format_weather_reply  # noqa: E402

SF_REPORT = (
    "Weather for San Francisco, California, United States of America "
    "(asked for: San Francisco; assumed San Francisco, California — say so if you "
    "meant a different one)\n"
    "  now: 61°F (16°C), Sunny, feels like 56°F, humidity 68%, wind 10 mph\n"
    "  forecast (2026-10-26): high 62°F / low 57°F, Sunny, 5% chance of rain\n"
    "  forecast (2026-10-27): high 61°F / low 55°F, Partly Cloudy, 8% chance of rain\n"
    "  forecast (2026-10-28): high 62°F / low 53°F, Sunny, 3% chance of rain")


# --- the trimmer -----------------------------------------------------------------------

def stream(*deltas):
    trimmer = AnswerEdgeTrimmer()
    return "".join(trimmer.delta(d) for d in deltas)


@pytest.mark.parametrize("deltas", [
    ("\n", "That's easy!"),
    ("\n\n\n", "That's easy!"),
    ("  \n", "\n\n", "That's easy!"),
    ("\nThat's", " easy!"),
    ("\n", "\n", "That's easy!\n", "\n  "),
])
def test_leading_and_trailing_blank_space_never_reaches_the_client(deltas):
    assert stream(*deltas).strip() == stream(*deltas) == "That's easy!"


def test_first_whitespace_only_deltas_emit_nothing():
    trimmer = AnswerEdgeTrimmer()
    assert [trimmer.delta(d) for d in ("\n", "\n\n", "  \n")] == ["", "", ""]


def test_internal_newlines_lists_and_code_survive():
    answer = ("Here you go:\n\n- one\n- two\n\n```python\nx = 1\n\n\ny = 2\n```\n"
              "Done.\n\nBye")
    for size in (1, 2, 5, len(answer)):
        chunks = [answer[i:i + size] for i in range(0, len(answer), size)]
        assert stream("\n\n", *chunks, "\n\n") == answer


def test_a_paragraph_break_split_across_deltas_is_kept():
    assert stream("\nFirst.", "\n", "\n", "Second.", "\n") == "First.\n\nSecond."


def test_reset_starts_a_fresh_answer():
    trimmer = AnswerEdgeTrimmer()
    assert trimmer.delta("\nLet me check.\n\n") == "Let me check."
    trimmer.reset()   # the loop discarded that preamble
    assert trimmer.delta("\n\n") == ""
    assert trimmer.delta("\nAnswer") == "Answer"


def test_a_whole_answer_is_trimmed_at_both_edges_only():
    assert AnswerEdgeTrimmer.whole("\n\n\nHi\n\n- a\n- b \n\n") == "Hi\n\n- a\n- b"


# --- weather formatter -----------------------------------------------------------------

def test_weather_reads_like_an_answer():
    reply = format_weather_reply(SF_REPORT)
    assert reply.startswith(
        "It's 61°F (16°C) and sunny in San Francisco right now — feels like 56°F. "
        "Today: high 62°F, low 57°F, 5% chance of rain. "
        "Tomorrow: partly cloudy, 61°F / 55°F.")
    assert reply.endswith("(I assumed San Francisco, California — say so if you "
                          "meant a different one.)")
    assert "\n" not in reply and "forecast (" not in reply and "humidity" not in reply


def test_no_assumption_note_when_nothing_was_assumed():
    report = SF_REPORT.replace(
        "asked for: San Francisco; assumed San Francisco, California — say so if "
        "you meant a different one", "asked for: San Francisco, CA")
    reply = format_weather_reply(report)
    assert "assumed" not in reply and reply.endswith("61°F / 55°F.")


def test_forecast_only_day_and_foreign_place():
    report = ("Weather for Dublin, Leinster, Ireland (asked for: Dublin) — tomorrow; "
              "forecast only\n  forecast (2026-10-27): high 50°F / low 40°F, "
              "Light rain, 60% chance of rain")
    assert format_weather_reply(report) == (
        "Tuesday in Dublin, Ireland: light rain, 50°F / 40°F, 60% chance of rain.")


def test_day_words_come_from_the_report_not_this_macs_clock():
    # The dates are far from any real "today" here: the first forecast day of a
    # report with current conditions is still that place's Today.
    tokyo = SF_REPORT.replace("2026-10-26", "2031-03-04").replace(
        "2026-10-27", "2031-03-05").replace("2026-10-28", "2031-03-06")
    reply = format_weather_reply(tokyo)
    assert "Today: high 62°F, low 57°F, 5% chance of rain. Tomorrow: partly cloudy" in reply
    # Non-consecutive rows are not "Tomorrow": the weekday is used instead.
    gap = SF_REPORT.replace("2026-10-27", "2026-10-29")
    assert "Thursday: partly cloudy" in format_weather_reply(gap)
    # A forecast-only report never uses a relative word.
    only = ("Weather for Tokyo, Tokyo, Japan (asked for: Tokyo) — today; forecast only\n"
            "  forecast (2026-10-27): high 70°F / low 60°F, Sunny, 0% chance of rain")
    assert format_weather_reply(only).startswith("Tuesday in Tokyo, Japan: sunny")


def test_a_later_day_mentions_rain_only_when_likely():
    report = SF_REPORT.replace("Partly Cloudy, 8%", "Rain, 70%")
    assert "Tomorrow: rain, 61°F / 55°F, 70% chance of rain." in format_weather_reply(
        report)


def test_missing_fields_degrade_gracefully():
    only_now = ("Weather for Paris, Ile-de-France, France (asked for: Paris)\n"
                "  now: 12°F")
    assert format_weather_reply(only_now) == "It's 12°F in Paris, France right now."
    unknown_rain = SF_REPORT.replace("5% chance of rain", "chance of rain unknown")
    assert "Today: high 62°F, low 57°F." in format_weather_reply(unknown_rain)
    no_feels = SF_REPORT.replace(", feels like 56°F", "")
    assert "feels like" not in format_weather_reply(no_feels)


@pytest.mark.parametrize("raw", [
    "", None, "(error: no location given — ask the user)", "Sunny and warm.",
    "Weather for X (asked for: X)\n  nothing useful here"])
def test_unreadable_reports_fall_back_to_none(raw):
    assert format_weather_reply(raw) is None


def test_one_line_optional_sections_are_kept_as_plain_lines():
    report = SF_REPORT + "\n  severe-weather alerts: No active severe-weather alerts."
    reply = format_weather_reply(report)
    assert reply.endswith("\n\nsevere-weather alerts: No active severe-weather alerts.")


@pytest.mark.parametrize("extra", [
    # A heading with a list under it: alert titles must never be hidden.
    "  severe-weather alerts: Active alerts:\n  Heat Advisory [Moderate] — until 8 PM",
    "  severe-weather alerts: Active alerts:\n  Flood Watch\n  (+3 more)",
    "  rain, next hours: Rain likely in the next 6h:\n  14:00   60% chance, 0.4mm",
    "  rain, next hours: Mostly dry for the next 6h:\n  14:00    5% chance",
    "  air quality: AQI 42 (Good).\n  something unexpected",
    "  unknown section: text",
    "(+3 more)",
])
def test_extras_the_formatter_cannot_fully_read_fall_back_to_the_raw_report(extra):
    assert format_weather_reply(SF_REPORT + "\n" + extra) is None


# --- the read path: sentence for the person, raw report for the model -------------------

def test_execute_read_shows_the_sentence_and_keeps_the_raw_tool_result(monkeypatch):
    from service.safety.policy import Decision, Tier
    from service.tools import web_tools
    from service.workflows import reads

    async def fake_yahoo(url):
        return SimpleNamespace(status_code=200, json=lambda: deepcopy({
            "nearest_area": [{"areaName": [{"value": "Paris"}],
                              "region": [{"value": "Ile-de-France"}],
                              "country": [{"value": "France"}]}],
            "current_condition": [{"temp_F": "50", "temp_C": "10",
                                   "weatherDesc": [{"value": "Cloudy"}],
                                   "FeelsLikeF": "47", "humidity": "70",
                                   "windspeedMiles": "5"}],
            "weather": [{"date": date.today().isoformat(), "maxtempF": "55",
                         "mintempF": "44", "hourly": [
                             {"weatherDesc": [{"value": "Cloudy"}], "chanceofrain": "10"}] * 5}],
        }))
    monkeypatch.setattr(web_tools, "_yahoo_get", fake_yahoo)
    monkeypatch.setattr(reads, "decide", lambda *_a, **_k: Decision(Tier.ALLOW, "test"))
    events = []

    async def emit(ev):
        events.append(ev)

    plan = reads.compile_read("What's the weather in Paris?")
    result = asyncio.run(reads.execute_read(plan, emit))
    assert result.status == "completed"
    assert result.response.startswith("It's 50°F (10°C) and cloudy in Paris, France right now")
    assert "Weather for" not in result.response
    raw = next(e["result"] for e in events if e["type"] == "tool_result")
    assert raw.startswith("Weather for Paris") and "humidity 70%" in raw
    assert result.tool_results[0]["result"] == raw


def test_a_failed_weather_read_keeps_its_plain_failure_reply(monkeypatch):
    from service.safety.policy import Decision, Tier
    from service.workflows import reads
    monkeypatch.setattr(reads, "decide", lambda *_a, **_k: Decision(Tier.ALLOW, "test"))

    async def emit(_ev):
        pass
    plan = ([("get_weather", {"location": "", "period": ""})], "")
    result = asyncio.run(reads.execute_read(plan, emit))
    assert result.status == "needs_input" and result.response.startswith("Which city")


# --- /agent end to end -----------------------------------------------------------------

@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    """Real /agent, routing and loop; scripted model output, no engine."""
    import httpx
    from service import main
    from service.agent import loop
    from service.memory import context
    from service.memory.store import SessionStore
    from service.router import router as R
    from service.safety.policy import Decision, Tier
    from service.workflows import reads

    scripts: list[list[str]] = []

    class Client:
        async def ensure_only(self, *_a, **_k):
            pass

        async def chat(self, *_a, **_k):
            raise AssertionError("unexpected internal model call")

        async def stream_events(self, _model, messages, **_k):
            deltas = scripts.pop(0) if scripts else ["Fine."]
            for d in deltas:
                yield {"kind": "content", "text": d}
            yield {"kind": "final", "message": {"role": "assistant",
                                                "content": "".join(deltas)}}

    async def no_engine():
        pass

    store = SessionStore(tmp_path / "endpoint.db")
    monkeypatch.setattr(main, "client", Client(), raising=False)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(context, "store", store)
    monkeypatch.setattr(main, "ensure_omlx", no_engine)
    config = lambda: {"tool_retrieval": {"provider": "lexical"}}
    monkeypatch.setattr(main, "models_config", config)
    monkeypatch.setattr(R, "models_config", config)
    monkeypatch.setattr(loop, "audit", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "decide", lambda *_a, **_k: Decision(Tier.ALLOW, "test"))
    monkeypatch.setattr(reads, "decide", lambda *_a, **_k: Decision(Tier.ALLOW, "test"))

    async def request(prompt):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://fixture.invalid") as http:
            response = await http.post("/agent", json={"prompt": prompt, "debug": False})
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ")]
        assert not [e for e in events if e["type"] == "error"], events
        return events, store

    return request, scripts


def shown(events):
    """The text a client would render: deltas, or the final text event."""
    return "".join(e["text"] for e in events if e["type"] in {"delta", "text"})


@pytest.mark.parametrize("prompt,kind", [
    ("Explain how photosynthesis works.", "delta"),            # streamed general reply
    ("Tell me something cheerful about autumn.", "text"),      # agent loop, one final text event
])
@pytest.mark.parametrize("deltas", [
    ["\n", "\n\n", "  \n", "Happy to help! ✨", " Here you go.\n", "\n"],
    ["\n\n\nThat's an easy one!", " 🎉\n\n- a\n- b\n"],
])
def test_agent_answer_has_no_blank_edges_and_keeps_the_middle(endpoint, prompt, kind, deltas):
    request, scripts = endpoint
    scripts.append(deltas)
    events, store = asyncio.run(request(prompt))
    text = shown(events)
    assert text and text == text.strip(), repr(text)
    assert text == "".join(deltas).strip()
    sent = [e["text"] for e in events if e["type"] == kind]
    assert sent and sent[0] == sent[0].lstrip()
    stored = [t for t in store.turns_from(events[0]["id"], 0) if t["role"] == "assistant"]
    assert stored and stored[-1]["content"] == text


def test_agent_weather_turn_final_text_is_the_sentence_form(endpoint, monkeypatch):
    from service.tools import web_tools

    async def fake_yahoo(url):
        return SimpleNamespace(status_code=200, json=lambda: deepcopy({
            "nearest_area": [{"areaName": [{"value": "San Francisco"}],
                              "region": [{"value": "California"}],
                              "country": [{"value": "United States of America"}]}],
            "current_condition": [{"temp_F": "61", "temp_C": "16",
                                   "weatherDesc": [{"value": "Sunny"}],
                                   "FeelsLikeF": "56", "humidity": "68",
                                   "windspeedMiles": "10"}],
            "weather": [
                {"date": date.today().isoformat(), "maxtempF": "62", "mintempF": "57",
                 "hourly": [{"weatherDesc": [{"value": "Sunny"}], "chanceofrain": "5"}] * 5},
                {"date": date.fromordinal(date.today().toordinal() + 1).isoformat(),
                 "maxtempF": "61", "mintempF": "55",
                 "hourly": [{"weatherDesc": [{"value": "Partly Cloudy"}],
                             "chanceofrain": "8"}] * 5}],
        }))
    monkeypatch.setattr(web_tools, "_yahoo_get", fake_yahoo)
    request, _scripts = endpoint
    events, store = asyncio.run(request("What's the weather like in San Francisco today?"))
    final = [e["text"] for e in events if e["type"] == "text"][-1]
    assert final.startswith("It's 61°F (16°C) and sunny in San Francisco right now — "
                            "feels like 56°F. Today: high 62°F, low 57°F, 5% chance of rain. "
                            "Tomorrow: partly cloudy, 61°F / 55°F.")
    assert "Weather for" not in final and "forecast (" not in final
    assert "I assumed San Francisco, California" in final
    raw = next(e["result"] for e in events if e["type"] == "tool_result")
    assert raw.startswith("Weather for San Francisco, California")   # model/follow-ups keep the data
    assert final == final.strip()
