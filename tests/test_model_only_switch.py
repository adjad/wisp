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
