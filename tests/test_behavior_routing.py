"""Behavior questions route to the behavior tools only while behavior learning
is on; with it off every phrase keeps its existing route."""
from __future__ import annotations

import asyncio

import pytest

import service.tools  # noqa: F401  (registers tools)
from service.router import route
from service.tools.registry import REGISTRY

NAMES = ("describe_me", "predict_my_day", "my_patterns", "correct_pattern")


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    for name in NAMES:   # routability follows the launch environment
        monkeypatch.setattr(REGISTRY[name], "unavailable_reason", "")


@pytest.fixture(autouse=True)
def _no_saved_setting(monkeypatch, tmp_path):
    from service import config
    monkeypatch.delenv("WISP_BEHAVIOR", raising=False)
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "cfg" / "config.yaml")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    yield
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


@pytest.fixture
def off(monkeypatch):
    from service.tools.behavior_tools import sync_availability
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    sync_availability()
    yield
    monkeypatch.delenv("WISP_BEHAVIOR")
    sync_availability()


def routed(text, **kw):
    return asyncio.run(route(text, **kw))


@pytest.mark.parametrize("text, tool, args", [
    ("what do you know about me", "describe_me", {}),
    ("What do you know about me?", "describe_me", {}),
    ("describe me", "describe_me", {}),
    ("what are my habits", "describe_me", {}),
    ("What have you learned about me so far?", "describe_me", {}),
    ("Hey Wisp, what habits have you noticed?", "describe_me", {}),
    ("predict my day tomorrow", "predict_my_day", {"day": "tomorrow"}),
    ("what will tomorrow look like", "predict_my_day", {"day": "tomorrow"}),
    ("what does tomorrow look like?", "predict_my_day", {"day": "tomorrow"}),
    ("can you predict today", "predict_my_day", {"day": "today"}),
    ("when do I usually work out", "my_patterns", {"query": "when do I usually work out"}),
    ("who do I talk to in the afternoon?", "my_patterns", {"query": "who do I talk to in the afternoon"}),
    ("which meetings do I usually cancel", "my_patterns", {"query": "which meetings do I usually cancel"}),
    # The week (predict_my_day covers only today/tomorrow): the learned weekly shape.
    ("what do you know about my week", "describe_me", {}),
    ("what's my week going to look like?", "describe_me", {}),
    ("What does my week look like", "describe_me", {}),
    ("how does my week look", "describe_me", {}),
])
def test_behavior_questions_route_to_behavior_tools(on, text, tool, args):
    d = routed(text)
    assert d.direct_calls == [(tool, args)], d.reason
    assert d.tool_subset == [tool]
    assert d.reason.startswith("behavior ")
    assert "get_upcoming" in d.forbidden_tools and "web_search" in d.forbidden_tools
    assert "not facts" in d.resolved_request


def test_predict_my_day_picks_a_day(on):
    [(name, args)] = routed("predict my day").direct_calls
    assert name == "predict_my_day" and args["day"] in {"today", "tomorrow"}


def test_correction_needs_pattern_word_or_prior_behavior_turn(on):
    d = routed("that pattern is wrong")
    assert set(d.tool_subset) == {"my_patterns", "correct_pattern"} and not d.direct_calls
    d = routed("that's wrong", last_tools="describe_me")
    assert d.reason == "behavior correction -> correct_pattern"
    assert routed("that's wrong").reason != "behavior correction -> correct_pattern"


@pytest.mark.parametrize("text", [
    "what's on my calendar tomorrow",
    "remind me to call mom at 5pm",
    "summarize my messages",
    "what do you know about me, and check my email",
    "when do I usually work out and remind me tonight",
    "cancel my 4pm meeting",
    "what's the weather tomorrow",
    "what's on my calendar this week",
    "what do I have this week",
    "what does my schedule look like this week",
])
def test_existing_routes_not_stolen(on, text):
    d = routed(text)
    assert not d.reason.startswith("behavior "), d.reason
    assert not any(name in NAMES for name, _ in d.direct_calls)


def test_compound_self_query_keeps_both_halves(on):
    tools = set(routed("what do you know about me, and check my email").tool_subset or ())
    assert "recall" in tools and "summarize_emails" in tools


def test_off_keeps_original_routes(off):
    d = routed("what do you know about me")
    assert d.reason == "self query -> recall" and d.force_first_tool == "recall"
    for text in ("describe me", "predict my day", "when do I usually work out"):
        d = routed(text)
        assert not d.reason.startswith("behavior ")
        offered = set(d.tool_subset or ()) | {n for n, _ in d.direct_calls}
        assert not offered & set(NAMES), (text, offered)


def test_week_questions_keep_their_routes_when_off(off):
    assert routed("what do you know about my week").reason == "self query -> recall"
    for text in ("what's my week going to look like", "what does my week look like"):
        d = routed(text)
        offered = set(d.tool_subset or ()) | {n for n, _ in d.direct_calls}
        assert not d.reason.startswith("behavior ") and not offered & set(NAMES), (text, offered)
