"""describe_me / predict_my_day / my_patterns / correct_pattern over a fake engine."""
from __future__ import annotations

import asyncio

import pytest

import service.demo.founder  # noqa: F401 — read its launch flags from the clean environment first
from service.assistant import behavior_runtime as br
from service.tools import behavior_tools as bt
from service.tools.registry import REGISTRY, classify_tool_outcome, run_tool
from tests.test_behavior_runtime import NOW, FakeEngine, make_runtime

NAMES = ("describe_me", "predict_my_day", "my_patterns", "correct_pattern")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("WISP_BEHAVIOR", "WISP_FOUNDER_DEMO", "WISP_BEHAVIOR_SYNTHETIC_ONLY"):
        monkeypatch.delenv(key, raising=False)
    br.set_runtime(None)
    yield
    br.set_runtime(None)


@pytest.fixture
def on(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    # Routability is fixed at import from the launch environment; mirror a launch with it on.
    for name in NAMES:
        monkeypatch.setattr(REGISTRY[name], "unavailable_reason", "")
    rt, engine = make_runtime(tmp_path)
    br.set_runtime(rt)
    return rt, engine


def call(name, args=None):
    return asyncio.run(run_tool(REGISTRY[name], args or {}))


def test_registered_read_only_and_hidden_when_off():
    from service.tools.registry import is_tool_routable
    for name in NAMES:
        tool = REGISTRY[name]
        assert tool.category == "assistant_read"
        assert not is_tool_routable(name)               # launched without behavior learning
        assert "Behavior learning is off" in call(name, {"status": "rejected"} if name == "correct_pattern" else {"query": "x"} if name == "my_patterns" else {})
    from service.safety.policy import Tier, decide
    assert decide("assistant_read", {}, "correct_pattern").tier is Tier.ALLOW


def test_describe_me_lists_statements_with_evidence(on):
    out = call("describe_me")
    assert "You go to the gym on weekday mornings (6:30-8:00)" in out
    assert "seen 14 of 16 times" in out and "confidence 72%" in out and "[pattern p_gym]" in out
    assert "Summary: Early riser" in out
    assert classify_tool_outcome("describe_me", out).status == "succeeded"
    gym_only = call("describe_me", {"subject": "work out"})
    assert "gym" in gym_only and "Sam" not in gym_only


def test_describe_me_hides_rejected_and_handles_empty(on):
    rt, engine = on
    engine.set_pattern_status("p_gym", "rejected")
    assert "[pattern p_gym]" not in call("describe_me")
    engine._patterns.clear()
    assert "haven't learned any reliable routines" in call("describe_me")


def test_predict_my_day(on, monkeypatch):
    rt, engine = on
    monkeypatch.setattr(rt, "clock", lambda: NOW)
    out = call("predict_my_day", {"day": "tomorrow"})
    assert out.startswith("Tomorrow (Tuesday, October 13)")
    assert "6:30 AM–8:00 AM gym — likely (82%)" in out
    assert "Team sync — may be cancelled (60%): last of 3 back-to-back meetings" in out
    assert "enough learned routine" in call("predict_my_day", {"day": "today"})


@pytest.mark.parametrize("query, expect", [
    ("when do I usually work out", "gym"),
    ("who do I talk to in the afternoon", "Sam"),
    ("which meetings do I usually cancel", "back-to-back"),
])
def test_my_patterns_answers_routine_questions(on, query, expect):
    out = call("my_patterns", {"query": query})
    assert out.startswith("Learned patterns matching") and expect in out


def test_my_patterns_no_match_lists_everything(on):
    out = call("my_patterns", {"query": "knitting"})
    assert "Nothing I've learned matches" in out and "gym" in out


def test_correct_pattern_by_id_and_text(on):
    rt, engine = on
    out = call("correct_pattern", {"pattern_id": "p_gym", "status": "rejected"})
    assert out.startswith("Got it") and engine._patterns["p_gym"].status == "rejected"
    out = call("correct_pattern", {"text": "Sam afternoons", "status": "confirmed"})
    assert out.startswith("Confirmed") and engine._patterns["p_sam"].status == "confirmed"
    amb = call("correct_pattern", {"text": "morning afternoon", "status": "rejected"})
    assert amb.startswith("Which one?")
    assert classify_tool_outcome("correct_pattern", amb).status == "needs_input"
    miss = call("correct_pattern", {"pattern_id": "zzz", "status": "rejected"})
    assert classify_tool_outcome("correct_pattern", miss).status == "no_match"
    assert "status must be" in call("correct_pattern", {"pattern_id": "p_gym", "status": "deleted"})


def test_remote_only_allowlist_tracks_behavior_flag(monkeypatch):
    from service.inference import remote_policy
    for name in NAMES:
        assert not remote_policy.remote_demo_tool_allowed(name)
    monkeypatch.setenv("WISP_FOUNDER_DEMO", "1")
    for name in NAMES:
        assert remote_policy.remote_demo_tool_allowed(name)
    monkeypatch.delenv("WISP_FOUNDER_DEMO")
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    assert all(remote_policy.remote_demo_tool_allowed(name) for name in NAMES)
    assert not remote_policy.remote_demo_tool_allowed("send_message")
    # The pinned four-read set itself is unchanged.
    assert "describe_me" not in remote_policy._READ_TOOLS


def test_schemas_fit_remote_gateway_budget():
    from service.inference.remote_policy import RemoteOnlyProfile
    schemas = [REGISTRY[n].schema() for n in NAMES]
    RemoteOnlyProfile().validate_payload({
        "model": "qwen3.6:35b", "stream": True, "max_tokens": 1024, "tools": schemas, "tool_choice": "auto",
        "messages": [{"role": "user", "content": "what do you know about me"}]})
