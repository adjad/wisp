"""The model-only test switch: off by default, env wins, bad values stay off."""
from __future__ import annotations

import pytest

from service import model_only as model_led


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    monkeypatch.setattr("service.config.models_config", lambda: {})
    assert model_led.model_only_enabled() is False


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("on", True),
                                            ("0", False), ("false", False), ("nope", False)])
def test_environment_wins(monkeypatch, value, expected):
    monkeypatch.setenv("WISP_MODEL_ONLY", value)
    monkeypatch.setattr("service.config.models_config", lambda: {"model_only": not expected})
    assert model_led.model_only_enabled() is expected


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("yes", True), (None, False)])
def test_config_key(monkeypatch, value, expected):
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    monkeypatch.setattr("service.config.models_config", lambda: {"model_only": value})
    assert model_led.model_only_enabled() is expected


def test_unreadable_config_stays_off(monkeypatch):
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)

    def boom():
        raise OSError("unreadable")
    monkeypatch.setattr("service.config.models_config", boom)
    assert model_led.model_only_enabled() is False


def test_packaged_default_is_off():
    import yaml
    from pathlib import Path
    packaged = yaml.safe_load((Path(__file__).resolve().parents[1] / "service/config/models.yaml").read_text())
    assert packaged.get("model_only") is False


def test_agent_steps_think_on_and_keep_profile_sampling(monkeypatch):
    from service.agent import loop
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    for model in ("Ling-3.0-tiny-Wisp-V3-merged-oQ4e", "Mellum2.1-12B-A2.5B-Thinking-oQ5e", "LFM2.5-2.6B-oQ6e"):
        for no_thinking in (False, True):
            assert loop._template_kwargs(model, no_thinking=no_thinking, think_on=False) == {
                "chat_template_kwargs": {"enable_thinking": True}}


def test_switch_off_keeps_the_normal_thinking_rules(monkeypatch):
    from service.agent import loop
    monkeypatch.setenv("WISP_MODEL_ONLY", "0")
    assert loop._template_kwargs("Ling-3.0-tiny-Wisp-V3-merged-oQ4e", no_thinking=False, think_on=False) == {
        "chat_template_kwargs": {"enable_thinking": False}}


def _reasoning_turn(monkeypatch, tmp_path, script, model_only="1"):
    import asyncio
    import json
    from service import diagnostics
    from service.agent import loop
    monkeypatch.setenv("WISP_MODEL_ONLY", model_only)
    monkeypatch.setattr(loop, "audit", lambda *a, **k: None)

    class Client:
        async def ensure_only(self, *a, **k):
            return None

        async def stream_events(self, model, messages, **kw):
            think, call, text = script.pop(0) if script else ("", None, "ok")
            if think:
                yield {"kind": "reasoning", "text": think}
            if call:
                yield {"kind": "final", "message": {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": call, "arguments": json.dumps({"period": "tomorrow"})}}]}}
            else:
                yield {"kind": "content", "text": text}
                yield {"kind": "final", "message": {"role": "assistant", "content": text, "tool_calls": None}}

    class Approver:
        async def confirm(self, action):
            return False

    events = []

    async def emit(ev):
        events.append(ev)

    async def go():
        with diagnostics.use(diagnostics.Trace("agent", root=tmp_path)):
            await loop.run_agent(Client(), "fixture-model", [{"role": "user", "content": "plan for tomorrow"}],
                                 emit, Approver(), tools=["get_upcoming"], test_mode=True, debug=False,
                                 include_memory_context=False, include_identity_context=False)
    asyncio.run(go())
    return [e for e in events if e["type"] in ("reasoning", "text", "done")]


def test_model_only_shows_reasoning_from_every_step(monkeypatch, tmp_path):
    out = _reasoning_turn(monkeypatch, tmp_path, [("Need the calendar.", "get_upcoming", ""),
                                                  ("Two events.", None, "You have two events.")])
    reasoning = [e["text"] for e in out if e["type"] == "reasoning"]
    assert reasoning == ["Step 1:\nNeed the calendar.\n\nStep 2:\nTwo events."]
    assert [e["type"] for e in out][-2:] == ["reasoning", "done"]


def test_model_only_shows_tool_step_reasoning_when_the_answer_step_has_none(monkeypatch, tmp_path):
    out = _reasoning_turn(monkeypatch, tmp_path, [("Need the calendar.", "get_upcoming", ""),
                                                  ("", None, "You have two events.")])
    assert [e["text"] for e in out if e["type"] == "reasoning"] == ["Need the calendar."]


def test_normal_path_still_shows_only_the_final_step(monkeypatch, tmp_path):
    out = _reasoning_turn(monkeypatch, tmp_path, [("Need the calendar.", "get_upcoming", ""),
                                                  ("", None, "You have two events.")], model_only="0")
    assert [e for e in out if e["type"] == "reasoning"] == []
