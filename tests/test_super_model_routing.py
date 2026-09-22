import asyncio
import sys
from types import SimpleNamespace

import service.inference.super_model as super_model


def decision(**overrides):
    values = {
        "needs_tools": False,
        "light_read": False,
        "direct_calls": [],
        "required_tool_groups": (),
        "tool_argument_bindings": {},
        "strict_read_limits": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def classify(prompt, route=None):
    return asyncio.run(super_model.cloud_super_model_eligible(
        prompt, route or decision()))


def test_high_confidence_standalone_generation_uses_cloud(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.01, 0.02, 0.01))
    assert classify("Explain how photosynthesis works.") == (
        True, "Laya classified this as standalone non-sensitive generation")


def test_laya_privacy_or_context_risk_stays_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.02, 0.01, 0.71))
    allowed, reason = classify("What about the second option?")
    assert allowed is False
    assert "conversation context" in reason


def test_every_tool_or_private_read_stays_local_without_calling_laya(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (_ for _ in ()).throw(AssertionError("called")))
    assert classify("What is on my calendar?", decision(needs_tools=True))[0] is False
    assert classify("Summarize the source", decision(light_read=True))[0] is False


def test_explicit_secrets_and_local_override_stay_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya", lambda prompt: (0.0, 0.0, 0.0))
    for prompt in (
        "My API key is sk-example and I need help.",
        "Read /Users/person/private.txt and explain it.",
        "Use only the local model for this request.",
        "Keep this on my machine.",
        "Stay local.",
        "Keep local.",
        "Local only, please.",
        "Review ghp_abcdefghijklmnopqrstuvwxyz123456.",
        "Explain /tmp/private-notes.txt.",
        "Open ./private-notes.txt.",
        "Read \"/Users/person/private.txt\" and explain it.",
        "Open `./private-notes.txt`.",
        "Never send this to the cloud.",
    ):
        assert classify(prompt)[0] is False


def test_laya_loader_pins_revision_and_uses_cpu_gpu(monkeypatch):
    calls = []
    fake_agent = object()
    fake_laya = SimpleNamespace(load=lambda model_id, **kwargs: (
        calls.append((model_id, kwargs)) or fake_agent))
    monkeypatch.setitem(sys.modules, "laya_coreml", fake_laya)
    monkeypatch.setattr(super_model, "_agent", None)
    monkeypatch.setattr(super_model, "_load_failed", False)

    assert super_model._load_agent() is fake_agent
    assert calls == [(super_model.LAYA_MODEL_ID, {
        "revision": super_model.LAYA_MODEL_REVISION,
        "compute_units": "cpu_gpu",
    })]


def test_missing_or_malformed_laya_fails_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(super_model, "start_laya_warmup", lambda: None)
    allowed, reason = classify("Explain a public scientific concept.")
    assert allowed is False
    assert "unavailable or uncertain" in reason
